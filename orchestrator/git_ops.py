import os
import subprocess
from pathlib import Path


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {cmd[1]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _run_lax(cmd: list[str]) -> str:
    """Like _run but returns empty string instead of raising on nothing-to-commit."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        combined = result.stdout + result.stderr
        if "nothing to commit" in combined or "nothing added to commit" in combined:
            return ""
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
    return _run_lax(["git", "commit", "-m", message])


def tag(name: str, message: str) -> None:
    _run(["git", "tag", "-a", name, "-m", message])


def push(branch: str, remote: str = "origin") -> None:
    _run(["git", "push", remote, branch])


def configure_remote_auth(repo: str, remote: str = "origin") -> None:
    """
    Set the remote URL to use the GITHUB_TOKEN for HTTPS auth.
    Call once on startup if GITHUB_TOKEN is set.
    repo format: "owner/repo-name"
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    _run(["git", "remote", "set-url", remote, url])


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
