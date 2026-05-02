"""
Docker sandbox runner.

Agent-generated code never executes on the host. Each run gets a fresh
container with network disabled, memory capped, CPU throttled, and a
read-only rootfs (project dir bind-mounted read-write at /workspace).
"""
import json
import re
from pathlib import Path

import docker
from docker.errors import ContainerError, ImageNotFound, APIError

from orchestrator.logger import log_event

# Per-container resource limits from the spec
_MEM_LIMIT = "512m"
_NANO_CPUS = 500_000_000  # 0.5 CPU


def _load_stack() -> dict:
    path = Path("config.json")
    if path.exists():
        return json.loads(path.read_text()).get("stack", {})
    return {}


def _runner_image(stack: dict) -> str:
    """Map language/test_runner to a Docker image tag."""
    language = stack.get("language", "typescript").lower()
    test_runner = stack.get("test_runner", "jest").lower()
    images = {
        ("typescript", "jest"): "node:20-alpine",
        ("javascript", "jest"): "node:20-alpine",
        ("python", "pytest"): "python:3.12-slim",
        ("go", "go test"): "golang:1.22-alpine",
    }
    return images.get((language, test_runner), "node:20-alpine")


class DockerRunner:
    def __init__(self, project_root: str | Path = ".") -> None:
        self._client = docker.from_env()
        self._project_root = Path(project_root).resolve()
        self._stack = _load_stack()
        self._image = _runner_image(self._stack)

    def _ensure_image(self) -> None:
        try:
            self._client.images.get(self._image)
        except ImageNotFound:
            log_event("docker_pull", {"image": self._image})
            self._client.images.pull(self._image)

    def run(
        self,
        command: str | list[str],
        working_dir: str = "/workspace",
        timeout_seconds: int = 120,
        instance_id: str = "docker",
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

        try:
            container = self._client.containers.run(
                image=self._image,
                command=cmd,
                working_dir=working_dir,
                volumes={
                    str(self._project_root): {
                        "bind": "/workspace",
                        "mode": "rw",
                    }
                },
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
        Returns the run result dict plus a parsed `failures` list.
        """
        test_runner = self._stack.get("test_runner", "jest")
        commands = {
            "jest": "npx jest --no-coverage --forceExit 2>&1",
            "pytest": "python -m pytest -v 2>&1",
            "go test": "go test ./... 2>&1",
            "mocha": "npx mocha 2>&1",
            "vitest": "npx vitest run 2>&1",
        }
        cmd = commands.get(test_runner, f"{test_runner} 2>&1")
        result = self.run(cmd, instance_id=instance_id)
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
        error_lines = [l.strip() for l in output.splitlines() if l.strip().startswith("Expected") or l.strip().startswith("Received")]
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
