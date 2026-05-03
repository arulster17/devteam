"""
GitHub REST client using PyGithub.

Orchestrator owns all GitHub operations; agents never call GitHub directly.
"""
import json
import time
from pathlib import Path

from github import Github, GithubException
from github.PullRequest import PullRequest
from github.Repository import Repository

from orchestrator.logger import log_event


def _load_config() -> dict:
    path = Path("config.json")
    return json.loads(path.read_text()) if path.exists() else {}


def _get_token() -> str:
    import os
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN environment variable not set")
    return token


class GitHubClient:
    def __init__(self) -> None:
        config = _load_config()
        self._config = config.get("github", {})
        self._limits = config.get("limits", {})
        self._gh = Github(_get_token())
        self._call_count = 0
        self._max_calls = self._limits.get("max_github_calls_per_run", 500)
        repo_name = self._config.get("repo", "")
        self._repo: Repository = self._gh.get_repo(repo_name)

    def _call(self, label: str):
        """Track call count against the per-run cap."""
        self._call_count += 1
        if self._call_count > self._max_calls:
            raise RuntimeError(
                f"GitHub call cap ({self._max_calls}) reached — aborting to prevent runaway usage"
            )
        log_event("github_call", {"label": label, "count": self._call_count})

    # ── Branch management ──────────────────────────────────────────────────

    def create_branch(self, branch_name: str, from_branch: str | None = None) -> None:
        self._call("create_branch")
        base = from_branch or self._config.get("main_branch", "main")
        ref = self._repo.get_branch(base)
        self._repo.create_git_ref(f"refs/heads/{branch_name}", ref.commit.sha)

    def branch_exists(self, branch_name: str) -> bool:
        self._call("branch_exists")
        try:
            self._repo.get_branch(branch_name)
            return True
        except GithubException:
            return False

    def get_default_branch(self) -> str:
        return self._config.get("main_branch", "main")

    # ── Issues ─────────────────────────────────────────────────────────────

    def create_milestone(self, title: str, description: str = "") -> int:
        self._call("create_milestone")
        milestone = self._repo.create_milestone(title=title, description=description)
        log_event("github_milestone_created", {"number": milestone.number, "title": title})
        return milestone.number

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        milestone_number: int | None = None,
    ) -> int:
        self._call("create_issue")
        kwargs: dict = {"title": title, "body": body}
        if labels:
            kwargs["labels"] = labels
        if milestone_number is not None:
            kwargs["milestone"] = self._repo.get_milestone(milestone_number)
        issue = self._repo.create_issue(**kwargs)
        log_event("github_issue_created", {"number": issue.number, "title": title})
        return issue.number

    def close_issue(self, number: int) -> None:
        self._call("close_issue")
        issue = self._repo.get_issue(number)
        issue.edit(state="closed")
        log_event("github_issue_closed", {"number": number})

    def get_open_issues(self) -> list[dict]:
        self._call("get_open_issues")
        issues = self._repo.get_issues(state="open")
        return [
            {"number": i.number, "title": i.title, "body": i.body or ""}
            for i in issues
        ]

    def add_issue_comment(self, number: int, body: str) -> None:
        self._call("add_issue_comment")
        issue = self._repo.get_issue(number)
        issue.create_comment(body)

    # ── Pull requests ──────────────────────────────────────────────────────

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
    ) -> int:
        self._call("create_pr")
        target = base or self._config.get("integration_branch", "integration")
        pr = self._repo.create_pull(title=title, body=body, head=head, base=target)
        log_event("github_pr_created", {"number": pr.number, "title": title, "head": head})
        return pr.number

    def get_pr(self, number: int) -> PullRequest:
        self._call("get_pr")
        return self._repo.get_pull(number)

    def get_pr_diff(self, number: int) -> str:
        """Return unified diff text for a PR."""
        self._call("get_pr_diff")
        pr = self._repo.get_pull(number)
        files = pr.get_files()
        parts = []
        for f in files:
            parts.append(f"--- a/{f.filename}\n+++ b/{f.filename}")
            parts.append(f.patch or "")
        return "\n".join(parts)

    def post_pr_review(
        self,
        pr_number: int,
        verdict: str,
        body: str,
        reviewer_id: str,
    ) -> None:
        """
        Post a GitHub PR review.
        verdict: "approve" | "request_changes" | "comment"
        """
        self._call("post_pr_review")
        event_map = {
            "approve": "APPROVE",
            "request_changes": "REQUEST_CHANGES",
            "comment": "COMMENT",
        }
        event = event_map.get(verdict.lower(), "COMMENT")
        pr = self._repo.get_pull(pr_number)
        pr.create_review(body=body, event=event)
        log_event("github_pr_review_posted", {
            "pr_number": pr_number,
            "verdict": verdict,
            "reviewer_id": reviewer_id,
        })

    def merge_pr(self, number: int, commit_message: str = "") -> None:
        self._call("merge_pr")
        pr = self._repo.get_pull(number)
        pr.merge(commit_message=commit_message, merge_method="squash")
        log_event("github_pr_merged", {"number": number})

    # ── Check / CI status polling ──────────────────────────────────────────

    def wait_for_checks(self, branch: str, timeout_seconds: int = 300) -> str:
        """
        Poll until all checks on `branch` complete.
        Returns "success", "failure", or "timeout".
        """
        poll_interval = self._limits.get("github_poll_interval_seconds", 15)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            self._call("get_commit_status")
            ref = self._repo.get_branch(branch)
            commit = self._repo.get_commit(ref.commit.sha)
            combined = commit.get_combined_status()
            if combined.state in ("success", "failure"):
                log_event("github_checks_done", {"branch": branch, "state": combined.state})
                return combined.state
            runs = list(commit.get_check_runs())
            if runs and all(r.status == "completed" for r in runs):
                conclusion = "success" if all(r.conclusion == "success" for r in runs) else "failure"
                log_event("github_checks_done", {"branch": branch, "state": conclusion})
                return conclusion
            time.sleep(poll_interval)
        log_event("github_checks_timeout", {"branch": branch})
        return "timeout"

    # ── Repository metadata ────────────────────────────────────────────────

    def get_repo_url(self) -> str:
        return self._repo.html_url

    def set_branch_protection(self, branch: str) -> None:
        """Enable basic branch protection (require PR reviews, no force-push)."""
        self._call("set_branch_protection")
        self._repo.get_branch(branch).edit_protection(
            required_approving_review_count=1,
            enforce_admins=False,
            allow_force_pushes=False,
        )
