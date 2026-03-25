"""Sandboxed tool execution for Ralph Loop phases.

Each tool execution is logged via the tracer. File operations are sandboxed
to the repo path. Shell commands run with configurable timeout and output capture.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import subprocess
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer

if TYPE_CHECKING:
    from engine.secrets import SecretRedactor

ToolFn = Callable[..., Coroutine[Any, Any, dict[str, Any]]]

DEFAULT_SHELL_TIMEOUT_S = 60
MAX_OUTPUT_BYTES = 100_000


class ToolError(Exception):
    """Raised when a tool execution fails due to invalid input or policy violation."""


class ToolExecutor:
    """Dispatches tool calls, enforces sandboxing, and records every execution."""

    def __init__(
        self,
        repo_path: str | Path,
        logger: StructuredLogger,
        tracer: Tracer,
        metrics: LoopMetrics,
        shell_timeout: int = DEFAULT_SHELL_TIMEOUT_S,
        allowed_tools: list[str] | None = None,
        redactor: SecretRedactor | None = None,
    ):
        self.repo_path = Path(repo_path).resolve()
        self.logger = logger
        self.tracer = tracer
        self.metrics = metrics
        self.shell_timeout = shell_timeout
        self._redactor = redactor

        self._registry: dict[str, ToolFn] = {
            "file_read": self._file_read,
            "file_write": self._file_write,
            "file_search": self._file_search,
            "shell_run": self._shell_run,
            "git_diff": self._git_diff,
            "git_commit": self._git_commit,
            "github_api": self._github_api,
        }

        if allowed_tools is not None:
            self._registry = {k: v for k, v in self._registry.items() if k in allowed_tools}

    @property
    def available_tools(self) -> list[str]:
        return list(self._registry.keys())

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Return LLM-friendly tool/function schemas for all available tools."""
        schemas: list[dict[str, Any]] = []
        for name in self._registry:
            schema = _TOOL_SCHEMAS.get(name)
            if schema:
                schemas.append(schema)
        return schemas

    async def execute(self, tool_name: str, **kwargs: Any) -> dict[str, Any]:
        """Execute a tool by name, log via tracer, return structured result."""
        if tool_name not in self._registry:
            raise ToolError(f"Unknown tool: {tool_name}. Available: {self.available_tools}")

        self.logger.debug(f"Tool call: {tool_name}", tool=tool_name, args=_safe_args(kwargs))

        with Tracer.timer() as timer:
            try:
                result = await self._registry[tool_name](**kwargs)
                success = result.get("success", True)
            except ToolError as exc:
                result = {"success": False, "error": str(exc)}
                success = False
            except Exception as exc:
                result = {"success": False, "error": f"Unexpected error: {exc}"}
                success = False

        if self._redactor:
            result = self._redactor.redact_dict(result)

        self.tracer.record_action(
            action_type=f"tool:{tool_name}",
            description=_describe_call(tool_name, kwargs),
            input_context=_safe_args(kwargs),
            output_success=success,
            output_data=_truncate_output(result),
            duration_ms=timer.elapsed_ms,
        )
        self.metrics.record_tool_execution()
        return result

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _file_read(self, path: str, **_: Any) -> dict[str, Any]:
        resolved = self._resolve_path(path)
        if not resolved.is_file():
            return {"success": False, "error": f"File not found: {path}"}
        content = resolved.read_text(errors="replace")
        if len(content) > MAX_OUTPUT_BYTES:
            content = content[:MAX_OUTPUT_BYTES] + "\n... [truncated]"
        rel_path = str(resolved.relative_to(self.repo_path))
        return {"success": True, "path": rel_path, "content": content}

    async def _file_write(self, path: str, content: str, **_: Any) -> dict[str, Any]:
        resolved = self._resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
        return {
            "success": True,
            "path": str(resolved.relative_to(self.repo_path)),
            "bytes_written": len(content.encode()),
        }

    async def _file_search(
        self,
        pattern: str,
        glob: str = "**/*",
        max_results: int = 50,
        **_: Any,
    ) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        for fpath in sorted(self.repo_path.rglob("*")):
            if not fpath.is_file():
                continue
            name_pattern = glob.split("/")[-1] if "/" not in glob else "*"
            rel_str = str(fpath.relative_to(self.repo_path))
            if not fnmatch.fnmatch(fpath.name, name_pattern) and not fnmatch.fnmatch(rel_str, glob):
                continue
            try:
                text = fpath.read_text(errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if pattern in line:
                    matches.append(
                        {
                            "file": str(fpath.relative_to(self.repo_path)),
                            "line": line_no,
                            "content": line.strip()[:200],
                        }
                    )
                    if len(matches) >= max_results:
                        return {"success": True, "matches": matches, "truncated": True}
        return {"success": True, "matches": matches, "truncated": False}

    async def _shell_run(
        self,
        command: str,
        timeout: int | None = None,
        working_dir: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        effective_timeout = timeout or self.shell_timeout
        cwd = self.repo_path
        if working_dir:
            cwd = self._resolve_path(working_dir)
            if not cwd.is_dir():
                return {"success": False, "error": f"Directory not found: {working_dir}"}

        try:
            proc = await asyncio.wait_for(
                _run_subprocess(command, cwd=str(cwd), timeout=effective_timeout),
                timeout=effective_timeout + 5,
            )
        except TimeoutError:
            return {
                "success": False,
                "error": f"Command timed out after {effective_timeout}s",
                "returncode": -1,
                "stdout": "",
                "stderr": "",
            }

        stdout = proc["stdout"]
        stderr = proc["stderr"]
        if len(stdout) > MAX_OUTPUT_BYTES:
            stdout = stdout[:MAX_OUTPUT_BYTES] + "\n... [truncated]"
        if len(stderr) > MAX_OUTPUT_BYTES:
            stderr = stderr[:MAX_OUTPUT_BYTES] + "\n... [truncated]"

        return {
            "success": proc["returncode"] == 0,
            "returncode": proc["returncode"],
            "stdout": stdout,
            "stderr": stderr,
        }

    async def _git_diff(
        self,
        ref: str = "HEAD",
        staged: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        cmd = "git diff"
        if staged:
            cmd += " --cached"
        else:
            cmd += f" {ref}"
        return await self._shell_run(command=cmd)

    async def _git_commit(
        self,
        message: str,
        files: list[str] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        if files:
            for f in files:
                add_result = await self._shell_run(command=f"git add {f}")
                if not add_result["success"]:
                    return {
                        "success": False,
                        "error": f"git add failed for {f}: {add_result.get('stderr', '')}",
                    }
        else:
            add_result = await self._shell_run(command="git add -A")
            if not add_result["success"]:
                err = add_result.get("stderr", "")
                return {"success": False, "error": f"git add -A failed: {err}"}

        safe_msg = message.replace('"', '\\"')
        return await self._shell_run(command=f'git commit -m "{safe_msg}"')

    async def _github_api(
        self,
        method: str = "GET",
        endpoint: str = "",
        body: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Call GitHub API via httpx. Requires GH_PAT or GITHUB_TOKEN env var."""
        import httpx

        token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
        if not token:
            return {"success": False, "error": "No GitHub token found (GH_PAT or GITHUB_TOKEN)"}

        url = endpoint if endpoint.startswith("http") else f"https://api.github.com{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.request(method, url, headers=headers, json=body)
                return {
                    "success": response.status_code < 400,
                    "status_code": response.status_code,
                    "body": response.json() if response.content else {},
                }
            except httpx.HTTPError as exc:
                return {"success": False, "error": f"HTTP error: {exc}"}

    # ------------------------------------------------------------------
    # Sandboxing helpers
    # ------------------------------------------------------------------

    def _resolve_path(self, relative_path: str) -> Path:
        """Resolve a path relative to repo_path, preventing path traversal."""
        resolved = (self.repo_path / relative_path).resolve()
        if not str(resolved).startswith(str(self.repo_path)):
            raise ToolError(f"Path traversal denied: {relative_path} resolves outside repo")
        return resolved


# ------------------------------------------------------------------
# Async subprocess helper
# ------------------------------------------------------------------


async def _run_subprocess(
    command: str,
    cwd: str,
    timeout: int,
) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        proc.communicate(),
        timeout=timeout,
    )
    return {
        "returncode": proc.returncode,
        "stdout": stdout_bytes.decode(errors="replace"),
        "stderr": stderr_bytes.decode(errors="replace"),
    }


# ------------------------------------------------------------------
# Utility helpers
# ------------------------------------------------------------------


def _safe_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of kwargs safe for logging (truncate large values)."""
    safe = {}
    for k, v in kwargs.items():
        if isinstance(v, str) and len(v) > 500:
            safe[k] = v[:500] + "... [truncated]"
        else:
            safe[k] = v
    return safe


def _describe_call(tool_name: str, kwargs: dict[str, Any]) -> str:
    if tool_name == "file_read":
        return f"Read file: {kwargs.get('path', '?')}"
    if tool_name == "file_write":
        return f"Write file: {kwargs.get('path', '?')}"
    if tool_name == "file_search":
        return f"Search for: {kwargs.get('pattern', '?')}"
    if tool_name == "shell_run":
        cmd = kwargs.get("command", "?")
        return f"Run: {cmd[:80]}"
    if tool_name == "git_diff":
        return f"Git diff ref={kwargs.get('ref', 'HEAD')}"
    if tool_name == "git_commit":
        return f"Git commit: {kwargs.get('message', '?')[:80]}"
    if tool_name == "github_api":
        return f"GitHub API {kwargs.get('method', 'GET')} {kwargs.get('endpoint', '?')}"
    return f"Tool: {tool_name}"


def _truncate_output(result: dict[str, Any]) -> dict[str, Any]:
    """Truncate large string values in result dict for tracer storage."""
    truncated: dict[str, Any] = {}
    for k, v in result.items():
        if isinstance(v, str) and len(v) > 2000:
            truncated[k] = v[:2000] + "... [truncated]"
        else:
            truncated[k] = v
    return truncated


# ------------------------------------------------------------------
# LLM-friendly tool schemas (for function-calling / tool-use APIs)
# ------------------------------------------------------------------

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "file_read": {
        "name": "file_read",
        "description": "Read the contents of a file relative to the repository root.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
            },
            "required": ["path"],
        },
    },
    "file_write": {
        "name": "file_write",
        "description": "Write content to a file relative to the repo root. Creates dirs.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    "file_search": {
        "name": "file_search",
        "description": "Search for a text pattern across files in the repository.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text pattern to search for"},
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (default: **/*)",
                    "default": "**/*",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matches to return",
                    "default": 50,
                },
            },
            "required": ["pattern"],
        },
    },
    "shell_run": {
        "name": "shell_run",
        "description": "Run a shell command in the repository directory. Output is captured.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 60)",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory relative to repo root",
                },
            },
            "required": ["command"],
        },
    },
    "git_diff": {
        "name": "git_diff",
        "description": "Get the git diff for the repository.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Git ref to diff against (default: HEAD)",
                    "default": "HEAD",
                },
                "staged": {
                    "type": "boolean",
                    "description": "Show staged changes only",
                    "default": False,
                },
            },
        },
    },
    "git_commit": {
        "name": "git_commit",
        "description": "Stage files and create a git commit.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to stage (default: all changes)",
                },
            },
            "required": ["message"],
        },
    },
    "github_api": {
        "name": "github_api",
        "description": "Call the GitHub REST API.",
        "parameters": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "description": "HTTP method",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                },
                "endpoint": {
                    "type": "string",
                    "description": "API endpoint (e.g. /repos/owner/name/issues/1)",
                },
                "body": {
                    "type": "object",
                    "description": "Request body for POST/PUT/PATCH",
                },
            },
            "required": ["endpoint"],
        },
    },
}
