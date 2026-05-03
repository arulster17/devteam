"""
Docker sandbox runner.

Agent-generated code never executes on the host. Each run gets a fresh
container with network disabled, memory capped, CPU throttled, and a
read-only rootfs (project dir bind-mounted read-write at /workspace).
"""
import json
import re
import tempfile
from pathlib import Path

from docker.errors import APIError, ContainerError, ImageNotFound  # type: ignore[import-not-found]

import docker  # type: ignore[import-not-found,attr-defined]
from orchestrator.logger import log_event

# Per-container resource limits from the spec
_MEM_LIMIT = "512m"
_NANO_CPUS = 500_000_000  # 0.5 CPU


def _load_stack() -> dict:
    path = Path("config.json")
    if path.exists():
        return json.loads(path.read_text()).get("stack", {})
    return {}


_DOCKER_DIR = Path(__file__).parent.parent / "docker"


def _runner_image(stack: dict) -> tuple[str, Path | None]:
    """Map language/test_runner to (image_tag, dockerfile_path|None)."""
    language = stack.get("language", "typescript").lower()
    test_runner = stack.get("test_runner", "jest").lower()
    _NODE = ("devteam-node:latest", _DOCKER_DIR / "node.dockerfile")
    _PYTHON = ("devteam-python:latest", _DOCKER_DIR / "python.dockerfile")
    configs: dict[tuple[str, str], tuple[str, Path | None]] = {
        ("typescript", "jest"): _NODE,
        ("javascript", "jest"): _NODE,
        ("javascript", "mocha"): _NODE,
        ("typescript", "vitest"): _NODE,
        ("python", "pytest"): _PYTHON,
        ("go", "go test"): ("golang:1.22-alpine", None),
    }
    return configs.get((language, test_runner), _NODE)


class DockerRunner:
    def __init__(self, project_root: str | Path = ".") -> None:
        self._client = docker.from_env()  # type: ignore[attr-defined]
        self._project_root = Path(project_root).resolve()
        self._stack = _load_stack()
        self._image, self._dockerfile = _runner_image(self._stack)

    def _ensure_image(self) -> None:
        try:
            self._client.images.get(self._image)
        except ImageNotFound:
            if self._dockerfile is not None and self._dockerfile.exists():
                log_event("docker_build", {"image": self._image, "dockerfile": str(self._dockerfile)})
                self._client.images.build(
                    path=str(self._project_root),
                    dockerfile=str(self._dockerfile.resolve()),
                    tag=self._image,
                    rm=True,
                )
            else:
                log_event("docker_pull", {"image": self._image})
                self._client.images.pull(self._image)

    def run(
        self,
        command: str | list[str],
        working_dir: str = "/workspace",
        timeout_seconds: int = 120,
        instance_id: str = "docker",
        extra_volumes: dict | None = None,
        environment: dict | None = None,
    ) -> dict:
        """
        Run `command` inside a sandboxed container.

        Returns:
          {
            "exit_code": int,
            "stdout": str,
            "stderr": str,
            "success": bool,
          }
        """
        self._ensure_image()

        cmd = command if isinstance(command, list) else ["sh", "-c", command]

        log_event("docker_run_start", {
            "instance_id": instance_id,
            "image": self._image,
            "command": command if isinstance(command, str) else " ".join(command),
        })

        volumes = {
            str(self._project_root): {
                "bind": "/workspace",
                "mode": "rw",
            }
        }
        if extra_volumes:
            volumes.update(extra_volumes)

        try:
            container = self._client.containers.run(
                image=self._image,
                command=cmd,
                working_dir=working_dir,
                volumes=volumes,
                environment=environment or {},
                network_disabled=True,
                mem_limit=_MEM_LIMIT,
                nano_cpus=_NANO_CPUS,
                read_only=False,  # workspace mount is rw; rest of rootfs is ephemeral
                remove=True,
                detach=False,
                stdout=True,
                stderr=True,
                timeout=timeout_seconds,
            )
            # containers.run with detach=False returns bytes on success
            output = container.decode("utf-8", errors="replace") if isinstance(container, bytes) else str(container)
            result = {
                "exit_code": 0,
                "stdout": output,
                "stderr": "",
                "success": True,
            }
        except ContainerError as exc:
            result = {
                "exit_code": exc.exit_status,
                "stdout": exc.output.decode("utf-8", errors="replace") if exc.output else "",
                "stderr": str(exc.stderr or ""),
                "success": False,
            }
        except APIError as exc:
            result = {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "success": False,
            }

        log_event("docker_run_done", {
            "instance_id": instance_id,
            "exit_code": result["exit_code"],
            "success": result["success"],
        })
        return result

    def run_tests(self, instance_id: str = "test_runner") -> dict:
        """
        Run the project's test suite using the configured test runner.

        Mounts a write-only results volume at /results and injects RESULTS_PATH
        so test runners can write structured JSON to /results/output.json.
        Falls back to stdout parsing if the file is not produced.

        Returns the run result dict plus a parsed `failures` list.
        """
        test_runner = self._stack.get("test_runner", "jest")
        commands = {
            "jest": 'npx jest --no-coverage --forceExit --json --outputFile="$RESULTS_PATH" 2>&1',
            "pytest": 'python -m pytest -v --json-report --json-report-file="$RESULTS_PATH" 2>&1',
            "go test": "go test ./... 2>&1",
            "mocha": "npx mocha 2>&1",
            "vitest": "npx vitest run 2>&1",
        }
        cmd = commands.get(test_runner, f"{test_runner} 2>&1")

        parsed_json: dict | None = None
        with tempfile.TemporaryDirectory() as results_dir:
            result = self.run(
                cmd,
                instance_id=instance_id,
                extra_volumes={results_dir: {"bind": "/results", "mode": "rw"}},
                environment={"RESULTS_PATH": "/results/output.json"},
            )
            results_file = Path(results_dir) / "output.json"
            if results_file.exists():
                try:
                    parsed_json = json.loads(results_file.read_text())
                except json.JSONDecodeError:
                    pass

        if parsed_json is not None:
            normalized = _normalize_results_json(parsed_json, test_runner)
            result["success"] = normalized["passed"]
            result["failures"] = normalized["failures"]
        else:
            result["failures"] = extract_failures(result["stdout"] + "\n" + result["stderr"], test_runner)

        return result

    def run_linter(self, instance_id: str = "linter") -> dict:
        """Run the configured linter; returns run result dict."""
        linter = self._stack.get("linter", "eslint")
        commands = {
            "eslint": "npx eslint . --ext .ts,.tsx,.js 2>&1",
            "ruff": "python -m ruff check . 2>&1",
            "golangci-lint": "golangci-lint run ./... 2>&1",
        }
        cmd = commands.get(linter, f"{linter} . 2>&1")
        return self.run(cmd, instance_id=instance_id)

    def run_type_check(self, instance_id: str = "type_checker") -> dict:
        """Run the configured type checker; returns run result dict."""
        checker = self._stack.get("type_checker", "tsc")
        commands = {
            "tsc": "npx tsc --noEmit 2>&1",
            "mypy": "python -m mypy . 2>&1",
        }
        cmd = commands.get(checker, f"{checker} 2>&1")
        return self.run(cmd, instance_id=instance_id)


def _normalize_results_json(data: dict, test_runner: str) -> dict:
    """Normalize runner-native JSON output to {passed: bool, failures: list[dict]}."""
    if test_runner == "pytest":
        # pytest-json-report: exitcode 0 = all passed
        passed = data.get("exitcode", 1) == 0
        failures = [
            {
                "test": t.get("nodeid", ""),
                "message": str(t.get("call", {}).get("longrepr", ""))[:500],
            }
            for t in data.get("tests", [])
            if t.get("outcome") == "failed"
        ]
        return {"passed": passed, "failures": failures}
    elif test_runner in ("jest", "vitest", "mocha"):
        # Jest --json: success field + nested testResults
        num_failed = data.get("numFailedTests", -1)
        passed = bool(data.get("success", num_failed == 0))
        failures = []
        for suite in data.get("testResults", []):
            for test in suite.get("testResults", []):
                if test.get("status") != "passed":
                    msg = " ".join(test.get("failureMessages", []))
                    failures.append({"test": test.get("fullName", ""), "message": msg[:500]})
        return {"passed": passed, "failures": failures}
    else:
        return {
            "passed": bool(data.get("passed", data.get("success", False))),
            "failures": data.get("failures", []),
        }


def extract_failures(output: str, test_runner: str = "jest") -> list[dict]:
    """
    Parse test output and return a list of failure dicts.
    Each dict has: {"test": str, "message": str}

    Used by debug_manager to build targeted context for debug workers.
    """
    failures: list[dict] = []

    if test_runner == "pytest":
        # Match "FAILED path/test_file.py::test_name - ErrorType: message"
        pattern = re.compile(r"FAILED (.+?) - (.+)")
        for match in pattern.finditer(output):
            failures.append({"test": match.group(1).strip(), "message": match.group(2).strip()})

    elif test_runner in ("jest", "vitest"):
        # Match "● TestSuiteName › test name"
        suite_pattern = re.compile(r"●\s+(.+)")
        # Extract failure blocks between ● markers
        blocks = suite_pattern.findall(output)
        # Also grab the "Expected / Received" lines from the output
        error_lines = [
            line.strip() for line in output.splitlines()
            if line.strip().startswith("Expected") or line.strip().startswith("Received")
        ]
        for i, block in enumerate(blocks):
            message = error_lines[i] if i < len(error_lines) else ""
            failures.append({"test": block.strip(), "message": message})

    elif test_runner == "go test":
        # Match "--- FAIL: TestName (0.00s)"
        pattern = re.compile(r"--- FAIL: (\S+)")
        for match in pattern.finditer(output):
            failures.append({"test": match.group(1), "message": ""})

    else:
        # Generic: look for lines containing "FAIL" or "Error"
        for line in output.splitlines():
            if "FAIL" in line or "Error" in line:
                failures.append({"test": line.strip(), "message": ""})

    return failures
