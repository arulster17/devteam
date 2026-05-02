import subprocess
from pathlib import Path


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {cmd[1]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def current_commit_hash() -> str:
    return _run(["git", "rev-parse", "HEAD"])


def create_branch(name: str) -> None:
    _run(["git", "checkout", "-b", name])


def checkout(branch: str) -> None:
    _run(["git", "checkout", branch])


def commit_all(message: str) -> str:
    _run(["git", "add", "-A"])
    return _run(["git", "commit", "-m", message])


def tag(name: str, message: str) -> None:
    _run(["git", "tag", "-a", name, "-m", message])


def write_gitignore() -> None:
    lines = [
        ".env",
        "__pycache__/",
        "*.pyc",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        "*.egg-info/",
    ]
    Path(".gitignore").write_text("\n".join(lines) + "\n")
    commit_all("chore: add .gitignore (.env excluded before anything else)")
