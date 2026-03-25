"""GitHub integration adapter — full GitHub REST API adapter.

Implements the IntegrationAdapter protocol (SPEC §9.2) with typed methods
for issues, PRs, comments, labels, CI status, and commit signing (gitsign).
Uses httpx for async HTTP. Token from GH_PAT or GITHUB_TOKEN env var.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any

import httpx

from engine.config import GitHubIntegrationConfig

API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_S = 30
MAX_SEARCH_RESULTS = 30


class GitHubAdapterError(Exception):
    """Raised for unrecoverable GitHub API errors."""


class GitHubAdapter:
    """Full GitHub REST API adapter implementing IntegrationAdapter.

    Provides high-level typed methods (``read_issue``, ``create_pr``, etc.)
    and the generic ``discover``/``read``/``write``/``search`` protocol.

    Resource IDs use ``type/identifier`` format:
    - ``issue/123`` — issue in the configured repo
    - ``pr/456`` — pull request
    - ``issue/123/comments`` — comments on an issue
    - ``pr/456/reviews`` — reviews on a PR
    - ``ci/ref/main`` — CI status for a git ref
    """

    name = "github"

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str | None = None,
        config: GitHubIntegrationConfig | None = None,
    ):
        self.owner = owner
        self.repo = repo
        self._token = token or os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
        self.config = config or GitHubIntegrationConfig()
        self._base_url = f"{API_BASE}/repos/{owner}/{repo}"

    @classmethod
    def from_issue_url(
        cls,
        issue_url: str,
        token: str | None = None,
        config: GitHubIntegrationConfig | None = None,
    ) -> GitHubAdapter:
        """Create an adapter by parsing an issue URL like
        ``https://github.com/owner/repo/issues/123``.
        """
        owner, repo = parse_repo_from_url(issue_url)
        return cls(owner=owner, repo=repo, token=token, config=config)

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # ------------------------------------------------------------------
    # Generic IntegrationAdapter protocol
    # ------------------------------------------------------------------

    async def discover(self) -> dict[str, Any]:
        """Check authentication and list capabilities."""
        capabilities = [
            "read_issue",
            "create_pr",
            "post_comment",
            "add_labels",
            "remove_label",
            "check_ci_status",
            "get_pr_reviews",
            "search_issues",
            "search_code",
        ]
        if self.config.commit_signing:
            capabilities.append("commit_signing")

        result: dict[str, Any] = {
            "name": self.name,
            "repo": self.repo_slug,
            "authenticated": False,
            "capabilities": capabilities,
        }

        if not self._token:
            result["error"] = "No GitHub token available (GH_PAT or GITHUB_TOKEN)"
            return result

        resp = await self._request("GET", f"{API_BASE}/user")
        if resp.get("success"):
            body = resp.get("body", {})
            result["authenticated"] = True
            result["user"] = body.get("login", "")
            result["scopes"] = resp.get("scopes", "")
        else:
            result["error"] = resp.get("error", "Authentication failed")

        return result

    async def read(self, resource_id: str) -> dict[str, Any]:
        """Read a resource by type/identifier.

        Supported formats:
        - ``issue/{number}`` — read an issue
        - ``pr/{number}`` — read a pull request
        - ``issue/{number}/comments`` — list comments on an issue
        - ``pr/{number}/reviews`` — list reviews on a PR
        - ``ci/ref/{ref}`` — combined CI status for a ref
        """
        parts = resource_id.strip("/").split("/")
        if len(parts) < 2:
            return {"success": False, "error": f"Invalid resource_id: {resource_id}"}

        rtype = parts[0]

        if rtype == "issue" and len(parts) == 2:
            return await self.read_issue(int(parts[1]))

        if rtype == "pr" and len(parts) == 2:
            return await self.read_pr(int(parts[1]))

        if rtype == "issue" and len(parts) == 3 and parts[2] == "comments":
            return await self.list_issue_comments(int(parts[1]))

        if rtype == "pr" and len(parts) == 3 and parts[2] == "reviews":
            return await self.get_pr_reviews(int(parts[1]))

        if rtype == "ci" and len(parts) >= 3 and parts[1] == "ref":
            ref = "/".join(parts[2:])
            return await self.check_ci_status(ref)

        return {"success": False, "error": f"Unknown resource format: {resource_id}"}

    async def write(self, resource_id: str, content: dict[str, Any]) -> dict[str, Any]:
        """Write to a resource by type/identifier.

        Supported formats:
        - ``pr`` — create a pull request (content: title, body, head, base)
        - ``issue/{number}/comments`` — post a comment (content: body)
        - ``issue/{number}/labels`` — add labels (content: labels list)
        - ``issue/{number}/labels/{label}`` — remove a label
        """
        parts = resource_id.strip("/").split("/")
        if not parts:
            return {"success": False, "error": f"Invalid resource_id: {resource_id}"}

        rtype = parts[0]

        if rtype == "pr" and len(parts) == 1:
            return await self.create_pr(
                title=content.get("title", ""),
                body=content.get("body", ""),
                head=content.get("head", ""),
                base=content.get("base", "main"),
            )

        if rtype == "issue" and len(parts) == 3 and parts[2] == "comments":
            return await self.post_comment(int(parts[1]), content.get("body", ""))

        if rtype == "issue" and len(parts) == 3 and parts[2] == "labels":
            return await self.add_labels(int(parts[1]), content.get("labels", []))

        if rtype == "issue" and len(parts) == 4 and parts[2] == "labels":
            return await self.remove_label(int(parts[1]), parts[3])

        return {"success": False, "error": f"Unknown write target: {resource_id}"}

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Search GitHub issues and code matching the query."""
        results: list[dict[str, Any]] = []

        issue_results = await self.search_issues(query)
        if issue_results.get("success"):
            for item in issue_results.get("items", []):
                results.append(
                    {
                        "type": "issue",
                        "number": item.get("number"),
                        "title": item.get("title", ""),
                        "url": item.get("html_url", ""),
                        "state": item.get("state", ""),
                    }
                )

        return results

    # ------------------------------------------------------------------
    # High-level typed methods
    # ------------------------------------------------------------------

    async def read_issue(self, number: int) -> dict[str, Any]:
        """Read a single issue by number."""
        resp = await self._request("GET", f"{self._base_url}/issues/{number}")
        if not resp.get("success"):
            return resp

        body = resp.get("body", {})
        return {
            "success": True,
            "number": body.get("number", number),
            "title": body.get("title", ""),
            "body": body.get("body", ""),
            "state": body.get("state", ""),
            "labels": [lb.get("name", "") for lb in body.get("labels", [])],
            "assignees": [a.get("login", "") for a in body.get("assignees", [])],
            "url": body.get("html_url", ""),
            "created_at": body.get("created_at", ""),
            "updated_at": body.get("updated_at", ""),
            "user": body.get("user", {}).get("login", ""),
        }

    async def read_pr(self, number: int) -> dict[str, Any]:
        """Read a single pull request by number."""
        resp = await self._request("GET", f"{self._base_url}/pulls/{number}")
        if not resp.get("success"):
            return resp

        body = resp.get("body", {})
        return {
            "success": True,
            "number": body.get("number", number),
            "title": body.get("title", ""),
            "body": body.get("body", ""),
            "state": body.get("state", ""),
            "head": body.get("head", {}).get("ref", ""),
            "base": body.get("base", {}).get("ref", ""),
            "mergeable": body.get("mergeable"),
            "url": body.get("html_url", ""),
            "diff_url": body.get("diff_url", ""),
            "user": body.get("user", {}).get("login", ""),
        }

    async def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> dict[str, Any]:
        """Create a pull request."""
        if not title or not head:
            return {"success": False, "error": "title and head are required"}

        resp = await self._request(
            "POST",
            f"{self._base_url}/pulls",
            json_body={"title": title, "body": body, "head": head, "base": base},
        )
        if not resp.get("success"):
            return resp

        pr_body = resp.get("body", {})
        return {
            "success": True,
            "number": pr_body.get("number", 0),
            "url": pr_body.get("html_url", ""),
            "state": pr_body.get("state", "open"),
        }

    async def post_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        """Post a comment on an issue or pull request."""
        if not body:
            return {"success": False, "error": "Comment body is required"}

        resp = await self._request(
            "POST",
            f"{self._base_url}/issues/{issue_number}/comments",
            json_body={"body": body},
        )
        if not resp.get("success"):
            return resp

        comment = resp.get("body", {})
        return {
            "success": True,
            "id": comment.get("id", 0),
            "url": comment.get("html_url", ""),
        }

    async def list_issue_comments(self, issue_number: int) -> dict[str, Any]:
        """List comments on an issue."""
        resp = await self._request("GET", f"{self._base_url}/issues/{issue_number}/comments")
        if not resp.get("success"):
            return resp

        raw_comments = resp.get("body", [])
        if not isinstance(raw_comments, list):
            raw_comments = []

        comments = [
            {
                "id": c.get("id", 0),
                "user": c.get("user", {}).get("login", ""),
                "body": c.get("body", ""),
                "created_at": c.get("created_at", ""),
            }
            for c in raw_comments
        ]
        return {"success": True, "comments": comments}

    async def add_labels(self, issue_number: int, labels: list[str]) -> dict[str, Any]:
        """Add labels to an issue or PR."""
        if not labels:
            return {"success": False, "error": "Labels list is empty"}

        resp = await self._request(
            "POST",
            f"{self._base_url}/issues/{issue_number}/labels",
            json_body={"labels": labels},
        )
        if not resp.get("success"):
            return resp

        applied = resp.get("body", [])
        if not isinstance(applied, list):
            applied = []
        return {
            "success": True,
            "labels": [lb.get("name", "") for lb in applied],
        }

    async def remove_label(self, issue_number: int, label: str) -> dict[str, Any]:
        """Remove a label from an issue or PR."""
        if not label:
            return {"success": False, "error": "Label name is required"}

        resp = await self._request(
            "DELETE",
            f"{self._base_url}/issues/{issue_number}/labels/{label}",
        )
        return {"success": resp.get("success", False), "label": label}

    async def check_ci_status(self, ref: str) -> dict[str, Any]:
        """Get combined CI status for a git ref (branch, tag, or SHA)."""
        resp = await self._request("GET", f"{self._base_url}/commits/{ref}/status")
        if not resp.get("success"):
            return resp

        body = resp.get("body", {})
        statuses = body.get("statuses", [])
        return {
            "success": True,
            "state": body.get("state", "unknown"),
            "total_count": body.get("total_count", 0),
            "statuses": [
                {
                    "context": s.get("context", ""),
                    "state": s.get("state", ""),
                    "description": s.get("description", ""),
                    "target_url": s.get("target_url", ""),
                }
                for s in statuses
            ],
            "sha": body.get("sha", ref),
        }

    async def get_pr_reviews(self, pr_number: int) -> dict[str, Any]:
        """List reviews on a pull request."""
        resp = await self._request("GET", f"{self._base_url}/pulls/{pr_number}/reviews")
        if not resp.get("success"):
            return resp

        raw_reviews = resp.get("body", [])
        if not isinstance(raw_reviews, list):
            raw_reviews = []

        reviews = [
            {
                "id": r.get("id", 0),
                "user": r.get("user", {}).get("login", ""),
                "state": r.get("state", ""),
                "body": r.get("body", ""),
                "submitted_at": r.get("submitted_at", ""),
            }
            for r in raw_reviews
        ]
        return {"success": True, "reviews": reviews}

    async def search_issues(
        self, query: str, max_results: int = MAX_SEARCH_RESULTS
    ) -> dict[str, Any]:
        """Search issues in this repository."""
        full_query = f"repo:{self.repo_slug} {query}"
        resp = await self._request(
            "GET",
            f"{API_BASE}/search/issues",
            params={"q": full_query, "per_page": str(min(max_results, 100))},
        )
        if not resp.get("success"):
            return resp

        body = resp.get("body", {})
        return {
            "success": True,
            "total_count": body.get("total_count", 0),
            "items": body.get("items", []),
        }

    # ------------------------------------------------------------------
    # Commit signing (gitsign / gpg)
    # ------------------------------------------------------------------

    async def configure_commit_signing(self, repo_path: str) -> dict[str, Any]:
        """Configure gitsign or GPG commit signing in the given repo.

        Uses the signing method from config (default: gitsign). In CI (GitHub
        Actions), gitsign authenticates via the ACTIONS_ID_TOKEN_REQUEST_URL.
        """
        method = self.config.signing_method

        if method == "gitsign":
            commands = [
                "git config commit.gpgsign true",
                "git config gpg.x509.program gitsign",
                "git config gpg.format x509",
            ]
        elif method == "gpg":
            commands = ["git config commit.gpgsign true"]
        else:
            return {"success": False, "error": f"Unknown signing method: {method}"}

        errors: list[str] = []
        for cmd in commands:
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=repo_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    errors.append(f"{cmd}: {stderr.decode(errors='replace').strip()}")
            except OSError as exc:
                errors.append(f"{cmd}: {exc}")

        if errors:
            return {"success": False, "errors": errors}
        return {"success": True, "method": method}

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated HTTP request to the GitHub API."""
        headers = self._headers()

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            try:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                )
                body: Any = {}
                if response.content:
                    try:
                        body = response.json()
                    except ValueError:
                        body = {"raw": response.text[:2000]}

                result: dict[str, Any] = {
                    "success": response.status_code < 400,
                    "status_code": response.status_code,
                    "body": body,
                }

                scopes = response.headers.get("x-oauth-scopes", "")
                if scopes:
                    result["scopes"] = scopes

                if response.status_code >= 400:
                    msg = body.get("message", "") if isinstance(body, dict) else str(body)
                    result["error"] = f"HTTP {response.status_code}: {msg}"

                return result
            except httpx.HTTPError as exc:
                return {"success": False, "error": f"HTTP error: {exc}"}


# ------------------------------------------------------------------
# URL parsing helpers
# ------------------------------------------------------------------


def parse_repo_from_url(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub URL.

    Accepts URLs like:
    - https://github.com/owner/repo/issues/123
    - https://github.com/owner/repo
    - github.com/owner/repo/pulls/456

    Raises ``ValueError`` if the URL cannot be parsed.
    """
    if "github.com/" not in url:
        raise ValueError(f"Not a GitHub URL: {url}")

    path = url.split("github.com/")[1].split("?")[0].split("#")[0]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Cannot extract owner/repo from URL: {url}")

    return parts[0], parts[1]


def parse_issue_number_from_url(url: str) -> int:
    """Extract the issue number from a GitHub issue URL.

    Raises ``ValueError`` if the URL does not contain an issue number.
    """
    if "/issues/" not in url:
        raise ValueError(f"Not a GitHub issue URL: {url}")

    try:
        return int(url.split("/issues/")[1].split("/")[0].split("?")[0])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"Cannot extract issue number from URL: {url}") from exc
