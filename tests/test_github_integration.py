"""Tests for engine.integrations.github — GitHubAdapter and helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.config import GitHubIntegrationConfig, load_config
from engine.integrations import IntegrationAdapter
from engine.integrations.github import (
    GitHubAdapter,
    GitHubAdapterError,
    parse_issue_number_from_url,
    parse_repo_from_url,
)

# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------


def _adapter(token: str = "test-token", **kwargs) -> GitHubAdapter:
    return GitHubAdapter(owner="test-org", repo="test-repo", token=token, **kwargs)


def _mock_response(
    status_code: int = 200,
    json_data: dict | list | None = None,
    headers: dict | None = None,
) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b'{"ok": true}' if json_data is not None else b""
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = str(json_data) if json_data is not None else ""
    resp.headers = headers or {}
    return resp


# ---------------------------------------------------------------
# IntegrationAdapter protocol compliance
# ---------------------------------------------------------------


class TestProtocolCompliance:
    def test_github_adapter_is_integration_adapter(self):
        adapter = _adapter()
        assert isinstance(adapter, IntegrationAdapter)

    def test_adapter_has_name(self):
        assert _adapter().name == "github"

    def test_adapter_has_all_protocol_methods(self):
        adapter = _adapter()
        assert hasattr(adapter, "discover")
        assert hasattr(adapter, "read")
        assert hasattr(adapter, "write")
        assert hasattr(adapter, "search")


# ---------------------------------------------------------------
# Constructor and properties
# ---------------------------------------------------------------


class TestConstructor:
    def test_basic_creation(self):
        adapter = GitHubAdapter(owner="octocat", repo="hello-world", token="tok")
        assert adapter.owner == "octocat"
        assert adapter.repo == "hello-world"
        assert adapter.repo_slug == "octocat/hello-world"

    def test_from_issue_url(self):
        adapter = GitHubAdapter.from_issue_url(
            "https://github.com/octocat/hello-world/issues/42", token="tok"
        )
        assert adapter.owner == "octocat"
        assert adapter.repo == "hello-world"

    def test_from_issue_url_with_config(self):
        cfg = GitHubIntegrationConfig(commit_signing=False)
        adapter = GitHubAdapter.from_issue_url(
            "https://github.com/o/r/issues/1", token="tok", config=cfg
        )
        assert adapter.config.commit_signing is False

    def test_token_from_env(self):
        with patch.dict("os.environ", {"GH_PAT": "env-token"}):
            adapter = GitHubAdapter(owner="o", repo="r")
            assert adapter._token == "env-token"

    def test_token_fallback_github_token(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "gh-tok"}, clear=False):
            env = {"GITHUB_TOKEN": "gh-tok"}
            with patch.dict("os.environ", env, clear=True):
                adapter = GitHubAdapter(owner="o", repo="r")
                assert adapter._token == "gh-tok"

    def test_explicit_token_overrides_env(self):
        with patch.dict("os.environ", {"GH_PAT": "env-token"}):
            adapter = GitHubAdapter(owner="o", repo="r", token="explicit")
            assert adapter._token == "explicit"

    def test_headers_include_auth(self):
        adapter = _adapter(token="my-token")
        headers = adapter._headers()
        assert headers["Authorization"] == "Bearer my-token"
        assert "application/vnd.github+json" in headers["Accept"]

    def test_headers_without_token(self):
        adapter = _adapter(token="")
        headers = adapter._headers()
        assert "Authorization" not in headers


# ---------------------------------------------------------------
# discover()
# ---------------------------------------------------------------


class TestDiscover:
    @pytest.mark.asyncio
    async def test_discover_no_token(self):
        adapter = _adapter(token="")
        result = await adapter.discover()
        assert result["authenticated"] is False
        assert "error" in result
        assert "capabilities" in result

    @pytest.mark.asyncio
    async def test_discover_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data={"login": "test-user"},
            headers={"x-oauth-scopes": "repo, read:org"},
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.discover()
        assert result["authenticated"] is True
        assert result["user"] == "test-user"
        assert "repo" in result
        assert "commit_signing" in result["capabilities"]

    @pytest.mark.asyncio
    async def test_discover_without_commit_signing(self):
        cfg = GitHubIntegrationConfig(commit_signing=False)
        adapter = _adapter(config=cfg)
        mock_resp = _mock_response(200, json_data={"login": "u"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.discover()
        assert "commit_signing" not in result["capabilities"]

    @pytest.mark.asyncio
    async def test_discover_auth_failure(self):
        adapter = _adapter()
        mock_resp = _mock_response(401, json_data={"message": "Bad credentials"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.discover()
        assert result["authenticated"] is False
        assert "error" in result


# ---------------------------------------------------------------
# read_issue()
# ---------------------------------------------------------------


class TestReadIssue:
    @pytest.mark.asyncio
    async def test_read_issue_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data={
                "number": 42,
                "title": "Bug: crash on startup",
                "body": "Steps to reproduce...",
                "state": "open",
                "labels": [{"name": "bug"}],
                "assignees": [{"login": "dev1"}],
                "html_url": "https://github.com/test-org/test-repo/issues/42",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "user": {"login": "reporter"},
            },
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.read_issue(42)

        assert result["success"] is True
        assert result["number"] == 42
        assert result["title"] == "Bug: crash on startup"
        assert result["labels"] == ["bug"]
        assert result["user"] == "reporter"

    @pytest.mark.asyncio
    async def test_read_issue_not_found(self):
        adapter = _adapter()
        mock_resp = _mock_response(404, json_data={"message": "Not Found"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.read_issue(999)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_read_issue_via_generic_read(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data={"number": 1, "title": "t", "body": "b"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.read("issue/1")
        assert result["success"] is True


# ---------------------------------------------------------------
# read_pr()
# ---------------------------------------------------------------


class TestReadPR:
    @pytest.mark.asyncio
    async def test_read_pr_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data={
                "number": 10,
                "title": "Fix: null check",
                "body": "Adds nil guard",
                "state": "open",
                "head": {"ref": "fix-branch"},
                "base": {"ref": "main"},
                "mergeable": True,
                "html_url": "https://github.com/test-org/test-repo/pull/10",
                "diff_url": "https://github.com/test-org/test-repo/pull/10.diff",
                "user": {"login": "author"},
            },
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.read_pr(10)

        assert result["success"] is True
        assert result["head"] == "fix-branch"
        assert result["mergeable"] is True

    @pytest.mark.asyncio
    async def test_read_pr_via_generic_read(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data={"number": 5, "title": "t"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.read("pr/5")
        assert result["success"] is True


# ---------------------------------------------------------------
# create_pr()
# ---------------------------------------------------------------


class TestCreatePR:
    @pytest.mark.asyncio
    async def test_create_pr_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            201,
            json_data={
                "number": 99,
                "html_url": "https://github.com/test-org/test-repo/pull/99",
                "state": "open",
            },
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.create_pr(
                title="Fix crash", body="Resolves #42", head="fix-branch", base="main"
            )

        assert result["success"] is True
        assert result["number"] == 99
        assert "pull/99" in result["url"]

    @pytest.mark.asyncio
    async def test_create_pr_missing_title(self):
        adapter = _adapter()
        result = await adapter.create_pr(title="", body="b", head="h")
        assert result["success"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_create_pr_missing_head(self):
        adapter = _adapter()
        result = await adapter.create_pr(title="t", body="b", head="")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_create_pr_via_generic_write(self):
        adapter = _adapter()
        mock_resp = _mock_response(201, json_data={"number": 1, "html_url": "u", "state": "open"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.write(
                "pr",
                {"title": "Fix", "body": "desc", "head": "branch"},
            )
        assert result["success"] is True


# ---------------------------------------------------------------
# post_comment()
# ---------------------------------------------------------------


class TestPostComment:
    @pytest.mark.asyncio
    async def test_post_comment_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            201,
            json_data={
                "id": 555,
                "html_url": "https://github.com/test-org/test-repo/issues/42#comment-555",
            },
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.post_comment(42, "Automated analysis complete.")

        assert result["success"] is True
        assert result["id"] == 555

    @pytest.mark.asyncio
    async def test_post_comment_empty_body(self):
        adapter = _adapter()
        result = await adapter.post_comment(42, "")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_post_comment_via_generic_write(self):
        adapter = _adapter()
        mock_resp = _mock_response(201, json_data={"id": 1, "html_url": "u"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.write("issue/42/comments", {"body": "hello"})
        assert result["success"] is True


# ---------------------------------------------------------------
# list_issue_comments()
# ---------------------------------------------------------------


class TestListIssueComments:
    @pytest.mark.asyncio
    async def test_list_comments_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data=[
                {
                    "id": 1,
                    "user": {"login": "user1"},
                    "body": "first comment",
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "id": 2,
                    "user": {"login": "user2"},
                    "body": "second comment",
                    "created_at": "2026-01-02T00:00:00Z",
                },
            ],
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.list_issue_comments(42)

        assert result["success"] is True
        assert len(result["comments"]) == 2
        assert result["comments"][0]["user"] == "user1"

    @pytest.mark.asyncio
    async def test_list_comments_via_generic_read(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data=[])
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.read("issue/1/comments")
        assert result["success"] is True
        assert result["comments"] == []

    @pytest.mark.asyncio
    async def test_list_comments_non_list_response(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data={"unexpected": "format"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.list_issue_comments(1)
        assert result["success"] is True
        assert result["comments"] == []


# ---------------------------------------------------------------
# add_labels() / remove_label()
# ---------------------------------------------------------------


class TestLabels:
    @pytest.mark.asyncio
    async def test_add_labels_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data=[{"name": "bug"}, {"name": "priority:high"}])
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.add_labels(42, ["bug", "priority:high"])
        assert result["success"] is True
        assert "bug" in result["labels"]

    @pytest.mark.asyncio
    async def test_add_labels_empty_list(self):
        adapter = _adapter()
        result = await adapter.add_labels(42, [])
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_add_labels_via_generic_write(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data=[{"name": "bug"}])
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.write("issue/42/labels", {"labels": ["bug"]})
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_add_labels_non_list_response(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data={"unexpected": True})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.add_labels(1, ["bug"])
        assert result["success"] is True
        assert result["labels"] == []

    @pytest.mark.asyncio
    async def test_remove_label_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data=[])
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.remove_label(42, "wontfix")
        assert result["success"] is True
        assert result["label"] == "wontfix"

    @pytest.mark.asyncio
    async def test_remove_label_empty_name(self):
        adapter = _adapter()
        result = await adapter.remove_label(42, "")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_remove_label_via_generic_write(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data=[])
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.write("issue/42/labels/wontfix", {})
        assert result["success"] is True


# ---------------------------------------------------------------
# check_ci_status()
# ---------------------------------------------------------------


class TestCIStatus:
    @pytest.mark.asyncio
    async def test_check_ci_status_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data={
                "state": "success",
                "total_count": 2,
                "statuses": [
                    {
                        "context": "ci/tests",
                        "state": "success",
                        "description": "All tests passed",
                        "target_url": "https://ci.example.com/1",
                    },
                    {
                        "context": "ci/lint",
                        "state": "success",
                        "description": "Lint clean",
                        "target_url": "https://ci.example.com/2",
                    },
                ],
                "sha": "abc123",
            },
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.check_ci_status("main")

        assert result["success"] is True
        assert result["state"] == "success"
        assert result["total_count"] == 2
        assert len(result["statuses"]) == 2

    @pytest.mark.asyncio
    async def test_check_ci_via_generic_read(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data={"state": "pending", "total_count": 0, "statuses": [], "sha": "x"},
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.read("ci/ref/main")
        assert result["success"] is True
        assert result["state"] == "pending"

    @pytest.mark.asyncio
    async def test_check_ci_with_slash_in_ref(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data={"state": "success", "total_count": 0, "statuses": [], "sha": "y"},
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.read("ci/ref/feature/my-branch")
        assert result["success"] is True


# ---------------------------------------------------------------
# get_pr_reviews()
# ---------------------------------------------------------------


class TestPRReviews:
    @pytest.mark.asyncio
    async def test_get_reviews_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data=[
                {
                    "id": 1,
                    "user": {"login": "reviewer"},
                    "state": "APPROVED",
                    "body": "LGTM",
                    "submitted_at": "2026-01-01T00:00:00Z",
                },
            ],
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.get_pr_reviews(10)

        assert result["success"] is True
        assert len(result["reviews"]) == 1
        assert result["reviews"][0]["state"] == "APPROVED"

    @pytest.mark.asyncio
    async def test_get_reviews_via_generic_read(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data=[])
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.read("pr/5/reviews")
        assert result["success"] is True
        assert result["reviews"] == []

    @pytest.mark.asyncio
    async def test_get_reviews_non_list_response(self):
        adapter = _adapter()
        mock_resp = _mock_response(200, json_data={"unexpected": True})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.get_pr_reviews(1)
        assert result["success"] is True
        assert result["reviews"] == []


# ---------------------------------------------------------------
# search()
# ---------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_issues_success(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data={
                "total_count": 1,
                "items": [
                    {
                        "number": 42,
                        "title": "Bug: crash",
                        "html_url": "https://github.com/test-org/test-repo/issues/42",
                        "state": "open",
                    }
                ],
            },
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter.search_issues("crash")

        assert result["success"] is True
        assert result["total_count"] == 1

    @pytest.mark.asyncio
    async def test_search_via_protocol(self):
        adapter = _adapter()
        mock_resp = _mock_response(
            200,
            json_data={
                "total_count": 1,
                "items": [
                    {
                        "number": 1,
                        "title": "Issue",
                        "html_url": "https://github.com/o/r/issues/1",
                        "state": "open",
                    }
                ],
            },
        )
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            results = await adapter.search("test query")
        assert len(results) == 1
        assert results[0]["type"] == "issue"

    @pytest.mark.asyncio
    async def test_search_api_failure(self):
        adapter = _adapter()
        mock_resp = _mock_response(403, json_data={"message": "Rate limited"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            results = await adapter.search("test")
        assert results == []


# ---------------------------------------------------------------
# Generic read/write error paths
# ---------------------------------------------------------------


class TestGenericReadWriteErrors:
    @pytest.mark.asyncio
    async def test_read_invalid_resource_id(self):
        adapter = _adapter()
        result = await adapter.read("invalid")
        assert result["success"] is False
        assert "Invalid resource_id" in result["error"]

    @pytest.mark.asyncio
    async def test_read_unknown_resource_type(self):
        adapter = _adapter()
        result = await adapter.read("unknown/123")
        assert result["success"] is False
        assert "Unknown resource format" in result["error"]

    @pytest.mark.asyncio
    async def test_write_empty_resource_id(self):
        adapter = _adapter()
        result = await adapter.write("", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_write_unknown_resource_type(self):
        adapter = _adapter()
        result = await adapter.write("unknown/thing", {})
        assert result["success"] is False
        assert "Unknown write target" in result["error"]


# ---------------------------------------------------------------
# Commit signing
# ---------------------------------------------------------------


class TestCommitSigning:
    @pytest.mark.asyncio
    async def test_configure_gitsign(self, tmp_path):
        adapter = _adapter()
        repo_path = str(tmp_path)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc) as mock_shell:
            result = await adapter.configure_commit_signing(repo_path)

        assert result["success"] is True
        assert result["method"] == "gitsign"
        assert mock_shell.call_count == 3

    @pytest.mark.asyncio
    async def test_configure_gpg(self, tmp_path):
        cfg = GitHubIntegrationConfig(signing_method="gpg")
        adapter = _adapter(config=cfg)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc) as mock_shell:
            result = await adapter.configure_commit_signing(str(tmp_path))

        assert result["success"] is True
        assert result["method"] == "gpg"
        assert mock_shell.call_count == 1

    @pytest.mark.asyncio
    async def test_configure_unknown_method(self, tmp_path):
        cfg = GitHubIntegrationConfig(signing_method="unknown")
        adapter = _adapter(config=cfg)
        result = await adapter.configure_commit_signing(str(tmp_path))
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_configure_command_failure(self, tmp_path):
        adapter = _adapter()

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error msg"))

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
            result = await adapter.configure_commit_signing(str(tmp_path))

        assert result["success"] is False
        assert "errors" in result
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    async def test_configure_os_error(self, tmp_path):
        adapter = _adapter()

        with patch("asyncio.create_subprocess_shell", side_effect=OSError("gitsign not found")):
            result = await adapter.configure_commit_signing(str(tmp_path))

        assert result["success"] is False
        assert "errors" in result


# ---------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------


class TestHTTPErrors:
    @pytest.mark.asyncio
    async def test_http_error_returns_failure(self):
        import httpx as httpx_mod

        adapter = _adapter()
        with patch(
            "httpx.AsyncClient.request",
            new_callable=AsyncMock,
            side_effect=httpx_mod.ConnectError("Connection refused"),
        ):
            result = await adapter.read_issue(42)
        assert result["success"] is False
        assert "HTTP error" in result["error"]

    @pytest.mark.asyncio
    async def test_non_json_response_body(self):
        adapter = _adapter()
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"plain text"
        resp.json.side_effect = ValueError("not json")
        resp.text = "plain text"
        resp.headers = {}

        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=resp):
            result = await adapter.read_issue(1)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_empty_response_body(self):
        adapter = _adapter()
        resp = MagicMock()
        resp.status_code = 204
        resp.content = b""
        resp.headers = {}

        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=resp):
            result = await adapter._request("DELETE", "https://api.github.com/test")
        assert result["success"] is True
        assert result["body"] == {}

    @pytest.mark.asyncio
    async def test_error_response_includes_message(self):
        adapter = _adapter()
        mock_resp = _mock_response(422, json_data={"message": "Validation Failed"})
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
            result = await adapter._request("POST", "https://api.github.com/test")
        assert result["success"] is False
        assert "422" in result["error"]
        assert "Validation Failed" in result["error"]


# ---------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------


class TestURLParsing:
    def test_parse_repo_from_issue_url(self):
        owner, repo = parse_repo_from_url("https://github.com/konflux-ci/build-service/issues/42")
        assert owner == "konflux-ci"
        assert repo == "build-service"

    def test_parse_repo_from_pr_url(self):
        owner, repo = parse_repo_from_url("https://github.com/octocat/hello-world/pull/1")
        assert owner == "octocat"
        assert repo == "hello-world"

    def test_parse_repo_bare_url(self):
        owner, repo = parse_repo_from_url("https://github.com/owner/repo")
        assert owner == "owner"
        assert repo == "repo"

    def test_parse_repo_with_query_params(self):
        owner, repo = parse_repo_from_url("https://github.com/o/r/issues/1?foo=bar#section")
        assert owner == "o"
        assert repo == "r"

    def test_parse_repo_not_github(self):
        with pytest.raises(ValueError, match="Not a GitHub URL"):
            parse_repo_from_url("https://gitlab.com/owner/repo")

    def test_parse_repo_too_short(self):
        with pytest.raises(ValueError, match="Cannot extract"):
            parse_repo_from_url("https://github.com/onlyowner")

    def test_parse_issue_number(self):
        num = parse_issue_number_from_url("https://github.com/o/r/issues/42")
        assert num == 42

    def test_parse_issue_number_with_query(self):
        num = parse_issue_number_from_url("https://github.com/o/r/issues/99?foo=bar")
        assert num == 99

    def test_parse_issue_number_no_issues(self):
        with pytest.raises(ValueError, match="Not a GitHub issue URL"):
            parse_issue_number_from_url("https://github.com/o/r/pull/1")

    def test_parse_issue_number_non_numeric(self):
        with pytest.raises(ValueError, match="Cannot extract"):
            parse_issue_number_from_url("https://github.com/o/r/issues/abc")


# ---------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------


class TestConfigIntegration:
    def test_default_integrations_config(self):
        config = load_config()
        assert config.integrations.github.enabled is True
        assert config.integrations.github.commit_signing is True
        assert config.integrations.github.signing_method == "gitsign"
        assert config.integrations.slack.enabled is False
        assert config.integrations.jira.enabled is False

    def test_yaml_overrides_integrations(self):
        overrides = {
            "integrations": {
                "github": {"commit_signing": False, "signing_method": "gpg"},
                "slack": {"enabled": True, "channel": "#alerts"},
                "jira": {"enabled": True, "project": "KFLUX"},
            }
        }
        config = load_config(overrides=overrides)
        assert config.integrations.github.commit_signing is False
        assert config.integrations.github.signing_method == "gpg"
        assert config.integrations.slack.enabled is True
        assert config.integrations.slack.channel == "#alerts"
        assert config.integrations.jira.enabled is True
        assert config.integrations.jira.project == "KFLUX"

    def test_partial_overrides_preserve_defaults(self):
        overrides = {"integrations": {"github": {"signing_method": "gpg"}}}
        config = load_config(overrides=overrides)
        assert config.integrations.github.enabled is True
        assert config.integrations.github.signing_method == "gpg"
        assert config.integrations.slack.enabled is False

    def test_unknown_integration_keys_ignored(self):
        overrides = {
            "integrations": {
                "github": {"nonexistent_key": "value"},
                "unknown_integration": {"foo": "bar"},
            }
        }
        config = load_config(overrides=overrides)
        assert config.integrations.github.enabled is True


# ---------------------------------------------------------------
# GitHubAdapterError
# ---------------------------------------------------------------


class TestAdapterError:
    def test_error_is_exception(self):
        err = GitHubAdapterError("something broke")
        assert isinstance(err, Exception)
        assert str(err) == "something broke"
