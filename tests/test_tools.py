"""Tests for the Tool Executor — sandboxed file, shell, git, and API operations."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer
from engine.tools.executor import ToolError, ToolExecutor


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with a sample file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.txt").write_text("Hello, world!\nLine two.\nLine three.\n")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('hello')\n")
    os.system(f"cd {repo} && git init -b main && git add -A && git commit -m 'init' --quiet")
    return repo


@pytest.fixture()
def executor(tmp_repo: Path) -> ToolExecutor:
    logger = StructuredLogger(execution_id="test-tools")
    tracer = Tracer()
    metrics = LoopMetrics()
    return ToolExecutor(
        repo_path=tmp_repo,
        logger=logger,
        tracer=tracer,
        metrics=metrics,
        shell_timeout=10,
    )


# ------------------------------------------------------------------
# available_tools / tool_schemas
# ------------------------------------------------------------------


def test_available_tools_lists_all(executor: ToolExecutor):
    tools = executor.available_tools
    assert "file_read" in tools
    assert "file_write" in tools
    assert "file_search" in tools
    assert "shell_run" in tools
    assert "git_diff" in tools
    assert "git_commit" in tools
    assert "github_api" in tools
    assert len(tools) == 7


def test_allowed_tools_filters(tmp_repo: Path):
    executor = ToolExecutor(
        repo_path=tmp_repo,
        logger=StructuredLogger(),
        tracer=Tracer(),
        metrics=LoopMetrics(),
        allowed_tools=["file_read", "file_search"],
    )
    assert executor.available_tools == ["file_read", "file_search"]


def test_tool_schemas_returns_valid_schemas(executor: ToolExecutor):
    schemas = executor.tool_schemas()
    assert len(schemas) == 7
    names = {s["name"] for s in schemas}
    assert "file_read" in names
    assert "shell_run" in names
    for s in schemas:
        assert "description" in s
        assert "parameters" in s


# ------------------------------------------------------------------
# file_read
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_read_success(executor: ToolExecutor):
    result = await executor.execute("file_read", path="hello.txt")
    assert result["success"] is True
    assert "Hello, world!" in result["content"]
    assert result["path"] == "hello.txt"


@pytest.mark.asyncio
async def test_file_read_nested(executor: ToolExecutor):
    result = await executor.execute("file_read", path="src/main.py")
    assert result["success"] is True
    assert "print('hello')" in result["content"]


@pytest.mark.asyncio
async def test_file_read_not_found(executor: ToolExecutor):
    result = await executor.execute("file_read", path="nonexistent.txt")
    assert result["success"] is False
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_file_read_path_traversal_blocked(executor: ToolExecutor):
    result = await executor.execute("file_read", path="../../etc/passwd")
    assert result["success"] is False
    err = result.get("error", "").lower()
    assert "traversal" in err or "denied" in err


# ------------------------------------------------------------------
# file_write
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_write_creates_new(executor: ToolExecutor, tmp_repo: Path):
    result = await executor.execute("file_write", path="new.txt", content="new content")
    assert result["success"] is True
    assert (tmp_repo / "new.txt").read_text() == "new content"


@pytest.mark.asyncio
async def test_file_write_creates_directories(executor: ToolExecutor, tmp_repo: Path):
    result = await executor.execute("file_write", path="a/b/c.txt", content="deep")
    assert result["success"] is True
    assert (tmp_repo / "a" / "b" / "c.txt").read_text() == "deep"


@pytest.mark.asyncio
async def test_file_write_overwrites(executor: ToolExecutor, tmp_repo: Path):
    await executor.execute("file_write", path="hello.txt", content="overwritten")
    assert (tmp_repo / "hello.txt").read_text() == "overwritten"


@pytest.mark.asyncio
async def test_file_write_path_traversal_blocked(executor: ToolExecutor):
    result = await executor.execute("file_write", path="../../evil.txt", content="bad")
    assert result["success"] is False


# ------------------------------------------------------------------
# file_search
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_search_finds_matches(executor: ToolExecutor):
    result = await executor.execute("file_search", pattern="Hello")
    assert result["success"] is True
    assert len(result["matches"]) >= 1
    assert result["matches"][0]["file"] == "hello.txt"
    assert result["matches"][0]["line"] == 1


@pytest.mark.asyncio
async def test_file_search_no_matches(executor: ToolExecutor):
    result = await executor.execute("file_search", pattern="ZZZZZZNOTFOUNDZZZZZ")
    assert result["success"] is True
    assert len(result["matches"]) == 0


@pytest.mark.asyncio
async def test_file_search_respects_max_results(executor: ToolExecutor, tmp_repo: Path):
    (tmp_repo / "many_lines.txt").write_text("\n".join(f"pattern_{i}" for i in range(100)))
    result = await executor.execute("file_search", pattern="pattern_", max_results=5)
    assert result["success"] is True
    assert len(result["matches"]) == 5
    assert result["truncated"] is True


# ------------------------------------------------------------------
# shell_run
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shell_run_success(executor: ToolExecutor):
    result = await executor.execute("shell_run", command="echo hello")
    assert result["success"] is True
    assert result["returncode"] == 0
    assert "hello" in result["stdout"]


@pytest.mark.asyncio
async def test_shell_run_captures_stderr(executor: ToolExecutor):
    result = await executor.execute("shell_run", command="echo err >&2")
    assert "err" in result["stderr"]


@pytest.mark.asyncio
async def test_shell_run_nonzero_exit(executor: ToolExecutor):
    result = await executor.execute("shell_run", command="exit 42")
    assert result["success"] is False
    assert result["returncode"] == 42


@pytest.mark.asyncio
async def test_shell_run_timeout(tmp_repo: Path):
    executor = ToolExecutor(
        repo_path=tmp_repo,
        logger=StructuredLogger(),
        tracer=Tracer(),
        metrics=LoopMetrics(),
        shell_timeout=1,
    )
    result = await executor.execute("shell_run", command="sleep 30", timeout=1)
    assert result["success"] is False
    assert "timed out" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_shell_run_working_dir(executor: ToolExecutor, tmp_repo: Path):
    result = await executor.execute("shell_run", command="pwd", working_dir="src")
    assert result["success"] is True
    assert "src" in result["stdout"]


@pytest.mark.asyncio
async def test_shell_run_bad_working_dir(executor: ToolExecutor):
    result = await executor.execute("shell_run", command="pwd", working_dir="nonexistent_dir")
    assert result["success"] is False
    assert "not found" in result["error"].lower()


# ------------------------------------------------------------------
# git_diff
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_diff_clean_repo(executor: ToolExecutor):
    result = await executor.execute("git_diff")
    assert result["success"] is True
    assert result["stdout"].strip() == ""


@pytest.mark.asyncio
async def test_git_diff_with_changes(executor: ToolExecutor, tmp_repo: Path):
    (tmp_repo / "hello.txt").write_text("modified content\n")
    result = await executor.execute("git_diff")
    assert result["success"] is True
    assert "modified content" in result["stdout"]


@pytest.mark.asyncio
async def test_git_diff_staged(executor: ToolExecutor, tmp_repo: Path):
    (tmp_repo / "hello.txt").write_text("staged change\n")
    os.system(f"cd {tmp_repo} && git add hello.txt")
    result = await executor.execute("git_diff", staged=True)
    assert result["success"] is True
    assert "staged change" in result["stdout"]


# ------------------------------------------------------------------
# git_commit
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_commit_all_changes(executor: ToolExecutor, tmp_repo: Path):
    (tmp_repo / "hello.txt").write_text("committed change\n")
    result = await executor.execute("git_commit", message="test commit")
    assert result["success"] is True
    assert "test commit" in result["stdout"] or result["returncode"] == 0


@pytest.mark.asyncio
async def test_git_commit_specific_files(executor: ToolExecutor, tmp_repo: Path):
    (tmp_repo / "hello.txt").write_text("changed\n")
    (tmp_repo / "other.txt").write_text("should not be committed\n")
    result = await executor.execute("git_commit", message="partial", files=["hello.txt"])
    assert result["success"] is True


# ------------------------------------------------------------------
# github_api (no real HTTP; tested for token handling)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_api_no_token(executor: ToolExecutor, monkeypatch):
    monkeypatch.delenv("GH_PAT", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = await executor.execute("github_api", endpoint="/repos/test/test")
    assert result["success"] is False
    assert "token" in result["error"].lower()


# ------------------------------------------------------------------
# Unknown tool
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_raises(executor: ToolExecutor):
    with pytest.raises(ToolError, match="Unknown tool"):
        await executor.execute("nonexistent_tool")


# ------------------------------------------------------------------
# Tracing integration
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_execution_traced(executor: ToolExecutor):
    await executor.execute("file_read", path="hello.txt")
    actions = executor.tracer.get_actions_as_dicts()
    assert len(actions) == 1
    assert actions[0]["action_type"] == "tool:file_read"
    assert actions[0]["output"]["success"] is True
    assert actions[0]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_metrics_incremented(executor: ToolExecutor):
    await executor.execute("shell_run", command="echo hi")
    assert executor.metrics.total_tool_executions == 1
    await executor.execute("shell_run", command="echo bye")
    assert executor.metrics.total_tool_executions == 2
