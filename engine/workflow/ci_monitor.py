"""CI monitoring and failure analysis for target-repo CI pipelines.

After the engine pushes a branch and creates a PR, the CIMonitor polls
the target repo's CI via the GitHub Checks API, downloads results, and
categorizes failures so the CI remediation loop (Phase 10.3) can take
targeted action.

Uses the GitHub REST API directly (httpx) rather than going through
GitHubAdapter because the monitor needs check-run-level detail (annotations,
log URLs, workflow run IDs) that the adapter's high-level methods don't expose.

Configuration flows from ``CIRemediationConfig`` in ``engine/config.py``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from engine.config import CIRemediationConfig
    from engine.observability.logger import StructuredLogger

API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_S = 30

INFRASTRUCTURE_KEYWORDS = frozenset(
    {
        "runner",
        "timed out",
        "timeout",
        "service unavailable",
        "network",
        "connection refused",
        "dns",
        "502",
        "503",
        "504",
        "rate limit",
        "could not resolve",
        "no space left",
        "out of memory",
        "oom",
        "exec format error",
        "permission denied: /var/run",
        "docker daemon",
        "container runtime",
    }
)

LINT_KEYWORDS = frozenset(
    {
        "lint",
        "linter",
        "eslint",
        "pylint",
        "ruff",
        "flake8",
        "golangci-lint",
        "stylelint",
        "rubocop",
        "checkstyle",
        "prettier",
        "format",
        "formatting",
        "clippy",
    }
)

BUILD_KEYWORDS = frozenset(
    {
        "build",
        "compile",
        "compilation",
        "compiler",
        "cannot find module",
        "undefined reference",
        "syntax error",
        "import error",
        "module not found",
        "type error",
        "typecheck",
        "tsc",
        "cargo build",
        "go build",
        "mvn compile",
        "gradle build",
    }
)


class CIFailureCategory(StrEnum):
    """Categorisation of CI failures for remediation strategy selection."""

    TEST_FAILURE = "test_failure"
    BUILD_ERROR = "build_error"
    LINT_VIOLATION = "lint_violation"
    INFRASTRUCTURE_FLAKE = "infrastructure_flake"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass
class CheckRunResult:
    """A single check run result from the GitHub Checks API."""

    id: int = 0
    name: str = ""
    status: str = ""  # queued, in_progress, completed
    conclusion: str = ""  # success, failure, cancelled, timed_out, action_required, ...
    html_url: str = ""
    details_url: str = ""
    output_title: str = ""
    output_summary: str = ""
    output_text: str = ""
    annotations: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "conclusion": self.conclusion,
            "html_url": self.html_url,
            "details_url": self.details_url,
            "output_title": self.output_title,
            "output_summary": self.output_summary,
            "output_text": self.output_text[:2000] if self.output_text else "",
            "annotations": self.annotations[:50],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass
class CIResult:
    """Aggregate CI result for a PR's head commit."""

    sha: str = ""
    overall_state: str = ""  # pending, success, failure, error
    check_runs: list[CheckRunResult] = field(default_factory=list)
    total_count: int = 0
    completed: bool = False
    workflow_run_ids: list[int] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "overall_state": self.overall_state,
            "check_runs": [cr.to_dict() for cr in self.check_runs],
            "total_count": self.total_count,
            "completed": self.completed,
            "workflow_run_ids": self.workflow_run_ids,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }

    @property
    def passed(self) -> bool:
        return self.completed and self.overall_state == "success"

    @property
    def failed_runs(self) -> list[CheckRunResult]:
        return [
            cr
            for cr in self.check_runs
            if cr.conclusion in ("failure", "cancelled", "timed_out", "action_required")
        ]


@dataclass
class FailureDetails:
    """Structured failure context for the CI remediation LLM."""

    category: CIFailureCategory = CIFailureCategory.UNKNOWN
    summary: str = ""
    failing_checks: list[str] = field(default_factory=list)
    error_messages: list[str] = field(default_factory=list)
    failing_tests: list[str] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)
    log_excerpts: list[str] = field(default_factory=list)
    workflow_run_ids: list[int] = field(default_factory=list)
    recommended_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "summary": self.summary,
            "failing_checks": self.failing_checks,
            "error_messages": self.error_messages[:20],
            "failing_tests": self.failing_tests[:50],
            "annotations": self.annotations[:30],
            "log_excerpts": [ex[:2000] for ex in self.log_excerpts[:10]],
            "workflow_run_ids": self.workflow_run_ids,
            "recommended_action": self.recommended_action,
        }


class CIMonitor:
    """Monitors and analyses CI status for a PR in the target repository.

    After the engine creates a PR, the CIMonitor:
    1. Polls check runs until all complete or timeout
    2. Downloads check run outputs, annotations, and workflow logs
    3. Categorises failures for the remediation loop
    4. Can trigger workflow reruns for infrastructure flakes

    The monitor is stateless per call — each ``poll_ci_status`` invocation
    starts fresh.  State between poll calls (e.g. tracking rerun counts)
    is managed by the caller (the CI remediation phase).
    """

    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        *,
        config: CIRemediationConfig | None = None,
        logger: StructuredLogger | None = None,
        api_base: str = API_BASE,
    ):
        self._token = token
        self._owner = owner
        self._repo = repo
        self._logger = logger
        self._api_base = api_base.rstrip("/")

        if config is not None:
            self._poll_interval = config.ci_poll_interval_seconds
            self._poll_timeout = config.ci_poll_timeout_minutes * 60
        else:
            self._poll_interval = 30
            self._poll_timeout = 20 * 60

    @property
    def repo_slug(self) -> str:
        return f"{self._owner}/{self._repo}"

    # ------------------------------------------------------------------
    # Core polling
    # ------------------------------------------------------------------

    async def poll_ci_status(
        self,
        ref: str,
        *,
        poll_interval: int | None = None,
        poll_timeout: int | None = None,
    ) -> CIResult:
        """Poll check runs for *ref* until all complete or timeout.

        *ref* is typically the branch name or head SHA of the PR.

        Returns a ``CIResult`` with ``completed=True`` when every check
        run has ``status == "completed"``, or ``completed=False`` on timeout.
        """
        interval = poll_interval if poll_interval is not None else self._poll_interval
        timeout = poll_timeout if poll_timeout is not None else self._poll_timeout

        start = time.monotonic()

        while True:
            ci = await self._fetch_check_runs(ref)
            ci.elapsed_seconds = time.monotonic() - start

            if ci.completed:
                if self._logger:
                    self._logger.info(
                        f"CI completed for {ref}: {ci.overall_state}",
                        ref=ref,
                        state=ci.overall_state,
                        elapsed_s=round(ci.elapsed_seconds, 1),
                    )
                return ci

            elapsed = time.monotonic() - start
            if elapsed > timeout:
                if self._logger:
                    self._logger.warn(
                        f"CI poll timeout after {elapsed:.0f}s for {ref}",
                        ref=ref,
                        elapsed_s=round(elapsed, 1),
                    )
                return ci

            if self._logger:
                pending = [cr.name for cr in ci.check_runs if cr.status != "completed"]
                self._logger.debug(
                    f"CI pending for {ref}, {len(pending)} check(s) still running",
                    pending_checks=pending[:10],
                )

            await asyncio.sleep(interval)

    async def _fetch_check_runs(self, ref: str) -> CIResult:
        """Fetch all check runs for a ref (single call, no polling)."""
        resp = await self._api_get(
            f"/repos/{self.repo_slug}/commits/{ref}/check-runs",
            params={"per_page": "100"},
        )

        if not resp.get("success"):
            return CIResult(
                sha=ref,
                overall_state="error",
                completed=False,
            )

        body = resp.get("body", {})
        raw_runs = body.get("check_runs", [])
        total = body.get("total_count", len(raw_runs))

        check_runs: list[CheckRunResult] = []
        workflow_run_ids: set[int] = set()
        all_completed = True
        any_failure = False

        for raw in raw_runs:
            output = raw.get("output", {}) or {}
            cr = CheckRunResult(
                id=raw.get("id", 0),
                name=raw.get("name", ""),
                status=raw.get("status", ""),
                conclusion=raw.get("conclusion", "") or "",
                html_url=raw.get("html_url", ""),
                details_url=raw.get("details_url", ""),
                output_title=output.get("title", "") or "",
                output_summary=output.get("summary", "") or "",
                output_text=output.get("text", "") or "",
                annotations=_extract_annotations(output),
                started_at=raw.get("started_at", ""),
                completed_at=raw.get("completed_at", ""),
            )
            check_runs.append(cr)

            if cr.status != "completed":
                all_completed = False
            if cr.conclusion in ("failure", "cancelled", "timed_out"):
                any_failure = True

            app = raw.get("app", {}) or {}
            if app.get("slug") == "github-actions":
                run_url = raw.get("details_url", "")
                run_id = _extract_run_id_from_url(run_url)
                if run_id:
                    workflow_run_ids.add(run_id)

        if total == 0:
            all_completed = False
            overall = "pending"
        elif not all_completed:
            overall = "pending"
        elif any_failure:
            overall = "failure"
        else:
            overall = "success"

        return CIResult(
            sha=ref,
            overall_state=overall,
            check_runs=check_runs,
            total_count=total,
            completed=all_completed and total > 0,
            workflow_run_ids=sorted(workflow_run_ids),
        )

    # ------------------------------------------------------------------
    # Result downloading
    # ------------------------------------------------------------------

    async def download_ci_results(self, ref: str) -> CIResult:
        """Fetch check runs with full output and annotations (no polling).

        Unlike ``poll_ci_status`` this is a single fetch, not a polling
        loop — use it after polling completes to get the final details.
        """
        ci = await self._fetch_check_runs(ref)

        for cr in ci.check_runs:
            if not cr.annotations and cr.id:
                annotations = await self._fetch_annotations(cr.id)
                cr.annotations = annotations

        return ci

    async def _fetch_annotations(self, check_run_id: int) -> list[dict[str, Any]]:
        """Fetch annotations for a specific check run."""
        resp = await self._api_get(
            f"/repos/{self.repo_slug}/check-runs/{check_run_id}/annotations",
            params={"per_page": "50"},
        )
        if not resp.get("success"):
            return []

        raw = resp.get("body", [])
        if not isinstance(raw, list):
            return []

        return [
            {
                "path": a.get("path", ""),
                "start_line": a.get("start_line", 0),
                "end_line": a.get("end_line", 0),
                "annotation_level": a.get("annotation_level", ""),
                "message": a.get("message", ""),
                "title": a.get("title", ""),
            }
            for a in raw
        ]

    async def download_workflow_log(self, workflow_run_id: int) -> str:
        """Download the combined log for a workflow run.

        GitHub returns a 302 redirect to a zip URL; httpx follows it.
        The raw text is returned (truncated to 50 KB for LLM context).
        """
        resp = await self._api_get(
            f"/repos/{self.repo_slug}/actions/runs/{workflow_run_id}/logs",
            accept="application/vnd.github+json",
        )
        if not resp.get("success"):
            return f"[failed to download log: {resp.get('error', 'unknown')}]"

        body = resp.get("body", "")
        if isinstance(body, dict):
            return body.get("raw", "")[:50000]
        return str(body)[:50000]

    # ------------------------------------------------------------------
    # Failure categorisation
    # ------------------------------------------------------------------

    def categorize_failure(self, ci_result: CIResult) -> CIFailureCategory:
        """Classify the primary failure category from CI results.

        Priority order: infrastructure flake > timeout > build error >
        lint violation > test failure > unknown.  This order ensures that
        retriable issues (flakes) are caught first, and code issues are
        categorised from most to least fundamental (build > lint > test).
        """
        if not ci_result.failed_runs:
            return CIFailureCategory.UNKNOWN

        all_text = _aggregate_failure_text(ci_result)
        lower_text = all_text.lower()

        if _matches_keywords(lower_text, INFRASTRUCTURE_KEYWORDS):
            return CIFailureCategory.INFRASTRUCTURE_FLAKE

        has_timed_out = any(cr.conclusion == "timed_out" for cr in ci_result.failed_runs)
        if has_timed_out:
            return CIFailureCategory.TIMEOUT

        if _matches_keywords(lower_text, BUILD_KEYWORDS):
            return CIFailureCategory.BUILD_ERROR

        if _matches_keywords(lower_text, LINT_KEYWORDS):
            return CIFailureCategory.LINT_VIOLATION

        if _has_test_signal(lower_text, ci_result):
            return CIFailureCategory.TEST_FAILURE

        return CIFailureCategory.UNKNOWN

    # ------------------------------------------------------------------
    # Failure detail extraction
    # ------------------------------------------------------------------

    def extract_failure_details(
        self,
        ci_result: CIResult,
        category: CIFailureCategory | None = None,
    ) -> FailureDetails:
        """Build structured failure context for the remediation LLM.

        Extracts failing check names, error messages, annotations, and
        log excerpts.  The result is sized for LLM context injection
        (truncated strings, capped lists).
        """
        if category is None:
            category = self.categorize_failure(ci_result)

        failed = ci_result.failed_runs
        failing_checks = [cr.name for cr in failed]

        error_messages: list[str] = []
        failing_tests: list[str] = []
        all_annotations: list[dict[str, Any]] = []
        log_excerpts: list[str] = []

        for cr in failed:
            if cr.output_title:
                error_messages.append(f"[{cr.name}] {cr.output_title}")
            if cr.output_summary:
                error_messages.append(cr.output_summary[:1000])

            for ann in cr.annotations:
                all_annotations.append(ann)
                msg = ann.get("message", "")
                level = ann.get("annotation_level", "")
                path = ann.get("path", "")
                line = ann.get("start_line", "")
                if level in ("failure", "error", "warning"):
                    error_messages.append(f"{path}:{line}: {msg}"[:500])

            if cr.output_text:
                log_excerpts.append(f"--- {cr.name} ---\n{cr.output_text[:3000]}")
                tests = _extract_failing_test_names(cr.output_text)
                failing_tests.extend(tests)

        action_map = {
            CIFailureCategory.TEST_FAILURE: "remediate",
            CIFailureCategory.BUILD_ERROR: "remediate",
            CIFailureCategory.LINT_VIOLATION: "remediate",
            CIFailureCategory.INFRASTRUCTURE_FLAKE: "rerun",
            CIFailureCategory.TIMEOUT: "escalate",
            CIFailureCategory.UNKNOWN: "remediate",
        }

        n_failed = len(failing_checks)
        summary = f"{n_failed} check(s) failed ({category.value}): {', '.join(failing_checks[:5])}"

        return FailureDetails(
            category=category,
            summary=summary,
            failing_checks=failing_checks,
            error_messages=error_messages[:20],
            failing_tests=failing_tests[:50],
            annotations=all_annotations[:30],
            log_excerpts=log_excerpts[:10],
            workflow_run_ids=ci_result.workflow_run_ids,
            recommended_action=action_map.get(category, "remediate"),
        )

    # ------------------------------------------------------------------
    # Workflow rerun
    # ------------------------------------------------------------------

    async def trigger_rerun(self, workflow_run_id: int) -> dict[str, Any]:
        """Re-trigger a workflow run (for infrastructure flakes).

        Uses POST /repos/{owner}/{repo}/actions/runs/{id}/rerun.
        Returns ``{"success": True, "run_id": ...}`` on success.
        """
        resp = await self._api_post(
            f"/repos/{self.repo_slug}/actions/runs/{workflow_run_id}/rerun",
        )
        if resp.get("success"):
            if self._logger:
                self._logger.info(
                    f"Triggered rerun for workflow run {workflow_run_id}",
                    workflow_run_id=workflow_run_id,
                )
            return {"success": True, "run_id": workflow_run_id}

        error = resp.get("error", "unknown")
        if self._logger:
            self._logger.warn(
                f"Failed to trigger rerun for {workflow_run_id}: {error}",
                workflow_run_id=workflow_run_id,
            )
        return {"success": False, "error": error, "run_id": workflow_run_id}

    async def trigger_rerun_failed_jobs(self, workflow_run_id: int) -> dict[str, Any]:
        """Re-trigger only failed jobs in a workflow run.

        More efficient than full rerun when some jobs passed.
        """
        resp = await self._api_post(
            f"/repos/{self.repo_slug}/actions/runs/{workflow_run_id}/rerun-failed-jobs",
        )
        if resp.get("success"):
            if self._logger:
                self._logger.info(
                    f"Triggered failed-job rerun for workflow run {workflow_run_id}",
                    workflow_run_id=workflow_run_id,
                )
            return {"success": True, "run_id": workflow_run_id}

        error = resp.get("error", "unknown")
        if self._logger:
            self._logger.warn(
                f"Failed to trigger failed-job rerun for {workflow_run_id}: {error}",
                workflow_run_id=workflow_run_id,
            )
        return {"success": False, "error": error, "run_id": workflow_run_id}

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _api_get(
        self,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> dict[str, Any]:
        url = f"{self._api_base}{endpoint}"
        headers = self._headers(accept)

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True) as client:
            try:
                response = await client.get(url, headers=headers, params=params)
                return self._parse_response(response)
            except httpx.HTTPError as exc:
                return {"success": False, "error": f"HTTP error: {exc}"}
            except Exception as exc:
                return {"success": False, "error": f"Unexpected error: {exc}"}

    async def _api_post(
        self,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._api_base}{endpoint}"
        headers = self._headers()

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True) as client:
            try:
                response = await client.post(url, headers=headers, json=json_body)
                return self._parse_response(response)
            except httpx.HTTPError as exc:
                return {"success": False, "error": f"HTTP error: {exc}"}
            except Exception as exc:
                return {"success": False, "error": f"Unexpected error: {exc}"}

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h: dict[str, str] = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        body: Any = {}
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = {"raw": response.text[:5000]}

        result: dict[str, Any] = {
            "success": response.status_code < 400,
            "status_code": response.status_code,
            "body": body,
        }
        if response.status_code >= 400:
            msg = body.get("message", "") if isinstance(body, dict) else str(body)[:200]
            result["error"] = f"HTTP {response.status_code}: {msg}"
        return result


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _extract_annotations(output: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract annotations from a check run's output object."""
    count = output.get("annotations_count", 0)
    if not count:
        return []
    annotations = output.get("annotations", [])
    if not isinstance(annotations, list):
        return []
    return [
        {
            "path": a.get("path", ""),
            "start_line": a.get("start_line", 0),
            "end_line": a.get("end_line", 0),
            "annotation_level": a.get("annotation_level", ""),
            "message": a.get("message", ""),
            "title": a.get("title", ""),
        }
        for a in annotations
    ]


def _extract_run_id_from_url(url: str) -> int | None:
    """Extract workflow run ID from a GitHub Actions details URL.

    Expected format: ``https://github.com/{owner}/{repo}/actions/runs/{id}/...``
    """
    if "/actions/runs/" not in url:
        return None
    try:
        segment = url.split("/actions/runs/")[1].split("/")[0]
        return int(segment)
    except (IndexError, ValueError):
        return None


def _aggregate_failure_text(ci_result: CIResult) -> str:
    """Combine all text from failed check runs for keyword matching."""
    parts: list[str] = []
    for cr in ci_result.failed_runs:
        parts.append(cr.name)
        parts.append(cr.output_title)
        parts.append(cr.output_summary)
        parts.append(cr.output_text[:5000])
        for ann in cr.annotations:
            parts.append(ann.get("message", ""))
    return "\n".join(parts)


def _matches_keywords(text: str, keywords: frozenset[str]) -> bool:
    """Check if any keyword appears in the (lowercased) text."""
    return any(kw in text for kw in keywords)


def _has_test_signal(lower_text: str, ci_result: CIResult) -> bool:
    """Detect whether the failure looks test-related."""
    test_words = {"test", "spec", "suite", "assert", "expect", "fail", "failed"}
    if any(w in lower_text for w in test_words):
        return True
    for cr in ci_result.failed_runs:
        name_lower = cr.name.lower()
        if any(w in name_lower for w in ("test", "spec", "check", "verify")):
            return True
    return False


def build_ci_pr_comment(history: CIRemediationHistory) -> str:
    """Build a structured PR comment summarising the CI remediation process.

    Produces a markdown comment covering three scenarios:
    - **Success**: which CI failures were encountered and how they were resolved
    - **Escalation**: full failure context formatted for human consumption
    - **Flake**: note that infrastructure flakes were detected and CI was re-run

    The comment is suitable for posting via ``GitHubAdapter.post_comment()``.
    """
    parts: list[str] = ["## CI Remediation Report", ""]

    if history.outcome == "success":
        parts.append(_format_success_comment(history))
    elif history.outcome in ("escalated", "timeout"):
        parts.append(_format_escalation_comment(history))
    else:
        parts.append(_format_generic_comment(history))

    if history.flake_reruns > 0:
        parts.append("")
        parts.append(_format_flake_section(history))

    parts.append("")
    parts.append(
        f"*CI monitoring ran {history.total_iterations} iteration(s) "
        f"in {_format_elapsed(history.elapsed_seconds)}.*"
    )
    parts.append("")
    parts.append("---")
    parts.append("*Posted by the Ralph Loop Engine — CI Remediation Phase*")

    return "\n".join(parts)


@dataclass
class CIRemediationAttempt:
    """One CI remediation attempt for comment reporting."""

    iteration: int = 0
    category: str = "unknown"
    summary: str = ""
    failing_checks: list[str] = field(default_factory=list)
    failing_tests: list[str] = field(default_factory=list)
    action_taken: str = ""
    files_changed: list[str] = field(default_factory=list)
    fix_pushed: bool = False
    success: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "category": self.category,
            "summary": self.summary,
            "failing_checks": self.failing_checks,
            "failing_tests": self.failing_tests,
            "action_taken": self.action_taken,
            "files_changed": self.files_changed,
            "fix_pushed": self.fix_pushed,
            "success": self.success,
        }


@dataclass
class CIRemediationHistory:
    """Aggregated CI remediation history for PR comment generation."""

    outcome: str = ""  # success, escalated, timeout
    total_iterations: int = 0
    flake_reruns: int = 0
    elapsed_seconds: float = 0.0
    attempts: list[CIRemediationAttempt] = field(default_factory=list)
    final_failure: FailureDetails | None = None
    escalation_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "total_iterations": self.total_iterations,
            "flake_reruns": self.flake_reruns,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "attempts": [a.to_dict() for a in self.attempts],
            "final_failure": self.final_failure.to_dict() if self.final_failure else None,
            "escalation_reason": self.escalation_reason,
        }


def _format_success_comment(history: CIRemediationHistory) -> str:
    """Format the comment body for a successful CI remediation."""
    parts: list[str] = []

    if not history.attempts:
        parts.append("CI passed without requiring remediation.")
        return "\n".join(parts)

    n = len(history.attempts)
    parts.append(
        f"CI initially failed but was **automatically resolved** after {n} remediation attempt(s)."
    )

    parts.append("")
    parts.append("### CI Failure History")
    parts.append("")

    for attempt in history.attempts:
        icon = "white_check_mark" if attempt.success else "x"
        parts.append(
            f"- :{icon}: **Attempt {attempt.iteration}** "
            f"({attempt.category}): {attempt.summary[:200]}"
        )
        if attempt.failing_tests:
            tests = ", ".join(f"`{t}`" for t in attempt.failing_tests[:5])
            n_extra = len(attempt.failing_tests) - 5
            extra = f" (+{n_extra} more)" if n_extra > 0 else ""
            parts.append(f"  - Failing tests: {tests}{extra}")
        if attempt.files_changed:
            files = ", ".join(f"`{f}`" for f in attempt.files_changed[:5])
            parts.append(f"  - Files fixed: {files}")
        if attempt.action_taken:
            parts.append(f"  - Action: {attempt.action_taken}")

    return "\n".join(parts)


def _format_escalation_comment(history: CIRemediationHistory) -> str:
    """Format the comment body when CI remediation was escalated to human."""
    parts: list[str] = []

    reason = history.escalation_reason or history.outcome
    parts.append(f"CI remediation was **escalated to human review**: {reason}")

    if history.attempts:
        parts.append("")
        parts.append("### What Was Tried")
        parts.append("")
        for attempt in history.attempts:
            icon = "white_check_mark" if attempt.success else "x"
            parts.append(
                f"- :{icon}: **Attempt {attempt.iteration}** "
                f"({attempt.category}): {attempt.summary[:200]}"
            )
            if attempt.files_changed:
                files = ", ".join(f"`{f}`" for f in attempt.files_changed[:5])
                parts.append(f"  - Files changed: {files}")
            if attempt.action_taken:
                parts.append(f"  - Action: {attempt.action_taken}")

    if history.final_failure:
        parts.append("")
        parts.append("### Current Failure Details")
        parts.append("")
        fd = history.final_failure
        parts.append(f"**Category**: {fd.category.value}")
        parts.append(f"**Summary**: {fd.summary[:300]}")

        if fd.failing_checks:
            checks = ", ".join(f"`{c}`" for c in fd.failing_checks[:10])
            parts.append(f"**Failing checks**: {checks}")

        if fd.failing_tests:
            parts.append("")
            parts.append("**Failing tests:**")
            for t in fd.failing_tests[:15]:
                parts.append(f"- `{t}`")

        if fd.error_messages:
            parts.append("")
            parts.append("<details><summary>Error messages</summary>")
            parts.append("")
            parts.append("```")
            for msg in fd.error_messages[:10]:
                parts.append(msg[:500])
            parts.append("```")
            parts.append("</details>")

        if fd.annotations:
            parts.append("")
            parts.append("<details><summary>CI annotations</summary>")
            parts.append("")
            for ann in fd.annotations[:10]:
                path = ann.get("path", "")
                line = ann.get("start_line", "")
                level = ann.get("annotation_level", "")
                msg = ann.get("message", "")
                parts.append(f"- `[{level}] {path}:{line}`: {msg[:200]}")
            parts.append("</details>")

    parts.append("")
    parts.append("### Suggestions for Manual Fix")
    parts.append("")
    if history.final_failure:
        parts.extend(_generate_suggestions(history.final_failure))
    else:
        parts.append("- Review the CI logs for the latest run")
        parts.append("- Check whether the failure is an infrastructure flake vs a code issue")

    return "\n".join(parts)


def _format_generic_comment(history: CIRemediationHistory) -> str:
    """Fallback comment body for unexpected outcomes."""
    return (
        f"CI remediation completed with outcome: **{history.outcome}**. "
        f"{history.total_iterations} iteration(s) attempted."
    )


def _format_flake_section(history: CIRemediationHistory) -> str:
    """Format the infrastructure flake section."""
    return (
        f"> **Infrastructure flakes detected**: CI was re-run "
        f"{history.flake_reruns} time(s) due to transient infrastructure issues "
        f"(network timeouts, runner failures, etc.)."
    )


def _generate_suggestions(fd: FailureDetails) -> list[str]:
    """Generate actionable suggestions based on the failure category."""
    suggestions: list[str] = []
    cat = fd.category

    if cat == CIFailureCategory.TEST_FAILURE:
        suggestions.append("- Review the failing test output for assertion mismatches")
        if fd.failing_tests:
            tests = ", ".join(f"`{t}`" for t in fd.failing_tests[:3])
            suggestions.append(f"- Start with: {tests}")
        suggestions.append("- Check whether the test expects behaviour changed by this PR")

    elif cat == CIFailureCategory.BUILD_ERROR:
        suggestions.append("- Check for missing imports or type errors in changed files")
        suggestions.append("- Verify dependency versions match the CI environment")

    elif cat == CIFailureCategory.LINT_VIOLATION:
        suggestions.append("- Run the linter locally to see the exact violations")
        suggestions.append("- The fix may need formatting adjustments")

    elif cat == CIFailureCategory.INFRASTRUCTURE_FLAKE:
        suggestions.append("- This appears to be an infrastructure issue, not a code problem")
        suggestions.append("- Try re-running the failed CI jobs")

    elif cat == CIFailureCategory.TIMEOUT:
        suggestions.append("- The CI pipeline timed out — check for long-running tests")
        suggestions.append("- Consider whether the change introduced performance regressions")

    else:
        suggestions.append("- Review the CI logs for the failing check(s)")
        suggestions.append("- Determine whether this is a code issue or environment issue")

    return suggestions


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    return f"{hours:.1f}h"


def _extract_failing_test_names(text: str) -> list[str]:
    """Best-effort extraction of individual failing test names.

    Looks for common patterns across Go, Python, JS, and Rust test output.
    """
    import re

    names: list[str] = []

    go_pattern = re.compile(r"---\s+FAIL:\s+(\S+)")
    names.extend(go_pattern.findall(text))

    pytest_pattern = re.compile(r"FAILED\s+([\w/.:]+(?:::\w+)*)")
    names.extend(pytest_pattern.findall(text))

    jest_pattern = re.compile(r"●\s+(.+?)$", re.MULTILINE)
    names.extend(jest_pattern.findall(text))

    rust_pattern = re.compile(r"test\s+([\w:]+)\s+\.\.\.\s+FAILED")
    names.extend(rust_pattern.findall(text))

    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        n = n.strip()
        if n and n not in seen:
            seen.add(n)
            unique.append(n)
    return unique
