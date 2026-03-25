"""Jira integration adapter — read issues, post comments, update status.

Implements the IntegrationAdapter protocol (SPEC §9.2) with typed methods
for reading issues, posting comments, transitioning issue status, and JQL
search. Uses httpx for async HTTP against the Jira REST API v2.

Supports both Jira Cloud (Basic auth with email:token) and Jira Data Center
(Bearer auth with PAT). Authentication mode is inferred from available
credentials: if both JIRA_API_TOKEN and JIRA_USER_EMAIL are set, Cloud mode
is used; if only JIRA_API_TOKEN is set, Data Center mode is used.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx

from engine.config import JiraIntegrationConfig

DEFAULT_TIMEOUT_S = 30
MAX_SEARCH_RESULTS = 50
MAX_COMMENT_RESULTS = 100

UNTRUSTED_CONTENT_DELIMITER = "--- UNTRUSTED JIRA CONTENT BELOW ---"


class JiraAdapterError(Exception):
    """Raised for unrecoverable Jira API errors."""


class JiraAdapter:
    """Jira REST API v2 adapter implementing IntegrationAdapter.

    Provides high-level typed methods (``read_issue``, ``post_comment``,
    ``transition_issue``, etc.) and the generic ``discover``/``read``/
    ``write``/``search`` protocol.

    Resource IDs use ``type/identifier`` format:
    - ``issue/{key}`` — read an issue (e.g., ``issue/PROJ-123``)
    - ``issue/{key}/comments`` — list or post comments
    - ``issue/{key}/transitions`` — list available transitions
    - ``issue/{key}/transition`` — transition to a new status

    All content read from Jira (issue descriptions, comments) is treated as
    untrusted input per SPEC §7 principle 3.
    """

    name = "jira"

    def __init__(
        self,
        server_url: str | None = None,
        token: str | None = None,
        email: str | None = None,
        config: JiraIntegrationConfig | None = None,
    ):
        self.config = config or JiraIntegrationConfig()
        self._server_url = (
            server_url or self.config.server_url or os.environ.get("JIRA_SERVER_URL", "")
        ).rstrip("/")
        self._token = token or os.environ.get("JIRA_API_TOKEN", "")
        self._email = email or os.environ.get("JIRA_USER_EMAIL", "")

    @property
    def is_cloud(self) -> bool:
        """True when both email and token are available (Jira Cloud basic auth)."""
        return bool(self._email and self._token)

    @property
    def base_api_url(self) -> str:
        return f"{self._server_url}/rest/api/2"

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.is_cloud:
            creds = base64.b64encode(f"{self._email}:{self._token}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        elif self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # ------------------------------------------------------------------
    # Generic IntegrationAdapter protocol
    # ------------------------------------------------------------------

    async def discover(self) -> dict[str, Any]:
        """Check authentication and list capabilities."""
        capabilities = [
            "read_issue",
            "post_comment",
            "list_comments",
            "get_transitions",
            "transition_issue",
            "search_issues",
        ]

        result: dict[str, Any] = {
            "name": self.name,
            "server_url": self._server_url,
            "authenticated": False,
            "auth_mode": "cloud" if self.is_cloud else "datacenter",
            "project": self.config.project,
            "capabilities": capabilities,
        }

        if not self._server_url:
            result["error"] = "No Jira server URL configured (JIRA_SERVER_URL or config)"
            return result

        if not self._token:
            result["error"] = "No Jira token available (JIRA_API_TOKEN)"
            return result

        resp = await self._request("GET", f"{self.base_api_url}/myself")
        if resp.get("success"):
            body = resp.get("body", {})
            result["authenticated"] = True
            result["user"] = body.get("displayName", "")
            result["email"] = body.get("emailAddress", "")
            result["account_id"] = body.get("accountId", body.get("key", ""))
        else:
            result["error"] = resp.get("error", "Authentication failed")

        return result

    async def read(self, resource_id: str) -> dict[str, Any]:
        """Read a resource by type/identifier.

        Supported formats:
        - ``issue/{key}`` — read an issue
        - ``issue/{key}/comments`` — list comments on an issue
        - ``issue/{key}/transitions`` — list available transitions
        """
        parts = resource_id.strip("/").split("/")
        if len(parts) < 2:
            return {"success": False, "error": f"Invalid resource_id: {resource_id}"}

        rtype = parts[0]

        if rtype == "issue" and len(parts) == 2:
            return await self.read_issue(parts[1])

        if rtype == "issue" and len(parts) == 3 and parts[2] == "comments":
            return await self.list_comments(parts[1])

        if rtype == "issue" and len(parts) == 3 and parts[2] == "transitions":
            return await self.get_transitions(parts[1])

        return {"success": False, "error": f"Unknown resource format: {resource_id}"}

    async def write(self, resource_id: str, content: dict[str, Any]) -> dict[str, Any]:
        """Write to a resource by type/identifier.

        Supported formats:
        - ``issue/{key}/comments`` — post a comment (content: body)
        - ``issue/{key}/transition`` — transition issue (content: transition_id)
        """
        parts = resource_id.strip("/").split("/")
        if not parts:
            return {"success": False, "error": f"Invalid resource_id: {resource_id}"}

        rtype = parts[0]

        if rtype == "issue" and len(parts) == 3 and parts[2] == "comments":
            return await self.post_comment(parts[1], content.get("body", ""))

        if rtype == "issue" and len(parts) == 3 and parts[2] == "transition":
            return await self.transition_issue(
                parts[1],
                content.get("transition_id", ""),
            )

        return {"success": False, "error": f"Unknown write target: {resource_id}"}

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Search Jira issues using the query as JQL.

        If ``config.project`` is set and the query doesn't already contain
        a ``project`` clause, the project is prepended automatically.
        """
        jql = query
        if self.config.project and "project" not in query.lower():
            jql = f"project = {self.config.project} AND ({query})"

        result = await self.search_issues(jql)
        if not result.get("success"):
            return []

        return [
            {
                "type": "issue",
                "key": issue.get("key", ""),
                "summary": issue.get("summary", ""),
                "status": issue.get("status", ""),
                "url": issue.get("url", ""),
            }
            for issue in result.get("issues", [])
        ]

    # ------------------------------------------------------------------
    # High-level typed methods
    # ------------------------------------------------------------------

    async def read_issue(self, issue_key: str) -> dict[str, Any]:
        """Read a single issue by key (e.g., ``PROJ-123``)."""
        if not issue_key:
            return {"success": False, "error": "Issue key is required"}

        resp = await self._request("GET", f"{self.base_api_url}/issue/{issue_key}")
        if not resp.get("success"):
            return resp

        body = resp.get("body", {})
        fields = body.get("fields", {})

        status_obj = fields.get("status", {})
        issuetype_obj = fields.get("issuetype", {})
        priority_obj = fields.get("priority", {})
        assignee_obj = fields.get("assignee") or {}
        reporter_obj = fields.get("reporter") or {}
        labels = fields.get("labels", [])
        components = [c.get("name", "") for c in fields.get("components", [])]

        return {
            "success": True,
            "key": body.get("key", issue_key),
            "summary": fields.get("summary", ""),
            "description": _wrap_untrusted(fields.get("description") or ""),
            "status": status_obj.get("name", "") if isinstance(status_obj, dict) else "",
            "issue_type": issuetype_obj.get("name", "") if isinstance(issuetype_obj, dict) else "",
            "priority": priority_obj.get("name", "") if isinstance(priority_obj, dict) else "",
            "assignee": assignee_obj.get("displayName", ""),
            "reporter": reporter_obj.get("displayName", ""),
            "labels": labels if isinstance(labels, list) else [],
            "components": components,
            "created": fields.get("created", ""),
            "updated": fields.get("updated", ""),
            "url": f"{self._server_url}/browse/{body.get('key', issue_key)}",
        }

    async def post_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        """Post a comment on an issue."""
        if not issue_key:
            return {"success": False, "error": "Issue key is required"}
        if not body:
            return {"success": False, "error": "Comment body is required"}

        resp = await self._request(
            "POST",
            f"{self.base_api_url}/issue/{issue_key}/comment",
            json_body={"body": body},
        )
        if not resp.get("success"):
            return resp

        comment = resp.get("body", {})
        return {
            "success": True,
            "id": comment.get("id", ""),
            "author": comment.get("author", {}).get("displayName", ""),
            "created": comment.get("created", ""),
        }

    async def list_comments(
        self,
        issue_key: str,
        max_results: int = MAX_COMMENT_RESULTS,
    ) -> dict[str, Any]:
        """List comments on an issue.

        All comment bodies are wrapped with injection guard delimiters,
        treating them as untrusted input per SPEC §7 principle 3.
        """
        if not issue_key:
            return {"success": False, "error": "Issue key is required"}

        resp = await self._request(
            "GET",
            f"{self.base_api_url}/issue/{issue_key}/comment",
            params={"maxResults": str(min(max_results, MAX_COMMENT_RESULTS))},
        )
        if not resp.get("success"):
            return resp

        body = resp.get("body", {})
        raw_comments = body.get("comments", [])
        if not isinstance(raw_comments, list):
            raw_comments = []

        comments = [
            {
                "id": c.get("id", ""),
                "author": c.get("author", {}).get("displayName", "")
                if isinstance(c.get("author"), dict)
                else "",
                "body": _wrap_untrusted(c.get("body", "")),
                "created": c.get("created", ""),
                "updated": c.get("updated", ""),
            }
            for c in raw_comments
        ]
        return {
            "success": True,
            "comments": comments,
            "total": body.get("total", len(comments)),
        }

    async def get_transitions(self, issue_key: str) -> dict[str, Any]:
        """List available status transitions for an issue."""
        if not issue_key:
            return {"success": False, "error": "Issue key is required"}

        resp = await self._request(
            "GET",
            f"{self.base_api_url}/issue/{issue_key}/transitions",
        )
        if not resp.get("success"):
            return resp

        body = resp.get("body", {})
        raw_transitions = body.get("transitions", [])
        if not isinstance(raw_transitions, list):
            raw_transitions = []

        transitions = [
            {
                "id": t.get("id", ""),
                "name": t.get("name", ""),
                "to": t.get("to", {}).get("name", "") if isinstance(t.get("to"), dict) else "",
            }
            for t in raw_transitions
        ]
        return {"success": True, "transitions": transitions}

    async def transition_issue(
        self,
        issue_key: str,
        transition_id: str,
    ) -> dict[str, Any]:
        """Transition an issue to a new status."""
        if not issue_key:
            return {"success": False, "error": "Issue key is required"}
        if not transition_id:
            return {"success": False, "error": "Transition ID is required"}

        resp = await self._request(
            "POST",
            f"{self.base_api_url}/issue/{issue_key}/transitions",
            json_body={"transition": {"id": transition_id}},
        )
        if not resp.get("success"):
            return resp

        return {
            "success": True,
            "key": issue_key,
            "transition_id": transition_id,
        }

    async def search_issues(
        self,
        jql: str,
        max_results: int = MAX_SEARCH_RESULTS,
    ) -> dict[str, Any]:
        """Search for issues using JQL."""
        if not jql:
            return {"success": False, "error": "JQL query is required"}

        resp = await self._request(
            "GET",
            f"{self.base_api_url}/search",
            params={
                "jql": jql,
                "maxResults": str(min(max_results, MAX_SEARCH_RESULTS)),
                "fields": "summary,status,issuetype,priority,assignee",
            },
        )
        if not resp.get("success"):
            return resp

        body = resp.get("body", {})
        raw_issues = body.get("issues", [])
        if not isinstance(raw_issues, list):
            raw_issues = []

        issues = [
            {
                "key": issue.get("key", ""),
                "summary": issue.get("fields", {}).get("summary", ""),
                "status": issue.get("fields", {}).get("status", {}).get("name", "")
                if isinstance(issue.get("fields", {}).get("status"), dict)
                else "",
                "issue_type": issue.get("fields", {}).get("issuetype", {}).get("name", "")
                if isinstance(issue.get("fields", {}).get("issuetype"), dict)
                else "",
                "priority": issue.get("fields", {}).get("priority", {}).get("name", "")
                if isinstance(issue.get("fields", {}).get("priority"), dict)
                else "",
                "url": f"{self._server_url}/browse/{issue.get('key', '')}",
            }
            for issue in raw_issues
        ]
        return {
            "success": True,
            "total": body.get("total", len(issues)),
            "issues": issues,
        }

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
        """Make an authenticated HTTP request to the Jira REST API."""
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

                if response.status_code >= 400:
                    msgs = body.get("errorMessages", []) if isinstance(body, dict) else []
                    msg = "; ".join(msgs) if msgs else str(body)[:200]
                    result["error"] = f"HTTP {response.status_code}: {msg}"

                return result
            except httpx.HTTPError as exc:
                return {"success": False, "error": f"HTTP error: {exc}"}


def _wrap_untrusted(text: str) -> str:
    """Wrap Jira content with untrusted content delimiters.

    Jira issue descriptions and comments are user-generated and must never
    be trusted as instructions. This wrapping makes it clear to LLM
    consumers that the content is untrusted.
    """
    if not text:
        return text
    return f"{UNTRUSTED_CONTENT_DELIMITER}\n{text}\n--- END UNTRUSTED JIRA CONTENT ---"
