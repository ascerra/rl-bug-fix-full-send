"""Self-monitoring for GitHub Actions workflow runs.

Detects if the engine is running inside GitHub Actions and provides methods
to query the current workflow run's status, job steps, and failure details.
This enables the loop to react to CI-level issues (e.g., cloning failures,
environment problems) that happen outside the engine's direct control.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from engine.observability.logger import StructuredLogger

CI_TIMEOUT_BUFFER_MINUTES = 15


@dataclass
class StepFailure:
    """Details of a failed workflow step."""

    name: str
    conclusion: str
    number: int
    started_at: str = ""
    completed_at: str = ""
    log_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "conclusion": self.conclusion,
            "number": self.number,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "log_excerpt": self.log_excerpt,
        }


@dataclass
class WorkflowContext:
    """Snapshot of GitHub Actions environment for the execution record."""

    is_ci: bool = False
    repository: str = ""
    run_id: str = ""
    run_url: str = ""
    job_name: str = ""
    run_number: str = ""
    run_attempt: str = ""
    actor: str = ""
    ref: str = ""
    sha: str = ""
    workflow: str = ""
    event_name: str = ""
    server_url: str = "https://github.com"

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_ci": self.is_ci,
            "repository": self.repository,
            "run_id": self.run_id,
            "run_url": self.run_url,
            "job_name": self.job_name,
            "run_number": self.run_number,
            "run_attempt": self.run_attempt,
            "actor": self.actor,
            "ref": self.ref,
            "sha": self.sha,
            "workflow": self.workflow,
            "event_name": self.event_name,
        }


@dataclass
class HealthCheck:
    """Result of a workflow health check."""

    healthy: bool = True
    run_status: str = ""
    failed_steps: list[StepFailure] = field(default_factory=list)
    context: WorkflowContext = field(default_factory=WorkflowContext)

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "run_status": self.run_status,
            "failed_steps": [s.to_dict() for s in self.failed_steps],
            "context": self.context.to_dict(),
        }


class WorkflowMonitor:
    """Monitors the current GitHub Actions workflow run.

    Created from environment variables when running in CI. Provides methods
    to query workflow status, detect step failures, and feed CI context into
    the loop's observation and execution record.

    When not running in GitHub Actions, `from_environment()` returns None and
    callers should treat monitoring as unavailable (all features are opt-in).
    """

    def __init__(
        self,
        token: str,
        repository: str,
        run_id: str,
        *,
        job_name: str = "",
        run_number: str = "",
        run_attempt: str = "",
        actor: str = "",
        ref: str = "",
        sha: str = "",
        workflow: str = "",
        event_name: str = "",
        server_url: str = "https://github.com",
        logger: StructuredLogger | None = None,
        api_base: str = "https://api.github.com",
    ):
        self._token = token
        self._repository = repository
        self._run_id = run_id
        self._job_name = job_name
        self._run_number = run_number
        self._run_attempt = run_attempt
        self._actor = actor
        self._ref = ref
        self._sha = sha
        self._workflow = workflow
        self._event_name = event_name
        self._server_url = server_url
        self._logger = logger
        self._api_base = api_base.rstrip("/")

    @classmethod
    def from_environment(
        cls,
        logger: StructuredLogger | None = None,
    ) -> WorkflowMonitor | None:
        """Create from GitHub Actions environment variables.

        Returns None if not running in GitHub Actions (GITHUB_ACTIONS != 'true')
        or if required variables (GITHUB_REPOSITORY, GITHUB_RUN_ID, token) are missing.
        """
        if os.environ.get("GITHUB_ACTIONS") != "true":
            return None

        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_PAT", "")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")

        if not token or not repository or not run_id:
            if logger:
                logger.warn(
                    "In GitHub Actions but missing GITHUB_TOKEN/GITHUB_REPOSITORY/GITHUB_RUN_ID"
                    " — self-monitoring disabled"
                )
            return None

        return cls(
            token=token,
            repository=repository,
            run_id=run_id,
            job_name=os.environ.get("GITHUB_JOB", ""),
            run_number=os.environ.get("GITHUB_RUN_NUMBER", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            actor=os.environ.get("GITHUB_ACTOR", ""),
            ref=os.environ.get("GITHUB_REF", ""),
            sha=os.environ.get("GITHUB_SHA", ""),
            workflow=os.environ.get("GITHUB_WORKFLOW", ""),
            event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
            server_url=os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
            logger=logger,
        )

    @property
    def is_github_actions(self) -> bool:
        return True

    @property
    def run_url(self) -> str:
        return f"{self._server_url}/{self._repository}/actions/runs/{self._run_id}"

    @property
    def context(self) -> WorkflowContext:
        """Snapshot of the CI environment for inclusion in execution records."""
        return WorkflowContext(
            is_ci=True,
            repository=self._repository,
            run_id=self._run_id,
            run_url=self.run_url,
            job_name=self._job_name,
            run_number=self._run_number,
            run_attempt=self._run_attempt,
            actor=self._actor,
            ref=self._ref,
            sha=self._sha,
            workflow=self._workflow,
            event_name=self._event_name,
            server_url=self._server_url,
        )

    async def get_run_status(self) -> dict[str, Any]:
        """Fetch the current workflow run status from the GitHub API."""
        endpoint = f"/repos/{self._repository}/actions/runs/{self._run_id}"
        return await self._api_get(endpoint)

    async def get_jobs(self) -> list[dict[str, Any]]:
        """Fetch all jobs for the current workflow run."""
        endpoint = f"/repos/{self._repository}/actions/runs/{self._run_id}/jobs"
        result = await self._api_get(endpoint)
        if not result.get("success"):
            return []
        return result.get("body", {}).get("jobs", [])

    async def get_failed_steps(self) -> list[StepFailure]:
        """Find failed steps across all jobs in the current workflow run.

        Steps with conclusion 'failure' or 'cancelled' are considered failed.
        """
        jobs = await self.get_jobs()
        failures: list[StepFailure] = []
        for job in jobs:
            for step in job.get("steps", []):
                conclusion = step.get("conclusion", "")
                if conclusion in ("failure", "cancelled"):
                    failures.append(
                        StepFailure(
                            name=step.get("name", "unknown"),
                            conclusion=conclusion,
                            number=step.get("number", 0),
                            started_at=step.get("started_at", ""),
                            completed_at=step.get("completed_at", ""),
                        )
                    )
        return failures

    async def get_job_log(self, job_id: int) -> str:
        """Fetch the log output for a specific job. Returns raw text or error message."""
        endpoint = f"/repos/{self._repository}/actions/jobs/{job_id}/logs"
        result = await self._api_get(endpoint, accept="application/vnd.github+json")
        if not result.get("success"):
            return f"[failed to fetch log: {result.get('error', 'unknown')}]"
        return result.get("body", "")

    async def check_health(self) -> HealthCheck:
        """Run a single health check: workflow status + any step failures.

        This is the main entry point for the loop to assess CI health at
        each iteration. Returns a HealthCheck with aggregated status.
        """
        ctx = self.context
        run_result = await self.get_run_status()
        run_status = ""
        if run_result.get("success"):
            body = run_result.get("body", {})
            run_status = body.get("status", "unknown")

        failed_steps = await self.get_failed_steps()
        healthy = len(failed_steps) == 0

        if self._logger:
            if healthy:
                self._logger.debug("Workflow health check: healthy", run_status=run_status)
            else:
                step_names = [s.name for s in failed_steps]
                self._logger.warn(
                    f"Workflow health check: {len(failed_steps)} failed step(s)",
                    failed_steps=step_names,
                    run_status=run_status,
                )

        return HealthCheck(
            healthy=healthy,
            run_status=run_status,
            failed_steps=failed_steps,
            context=ctx,
        )

    async def _api_get(
        self,
        endpoint: str,
        accept: str = "application/vnd.github+json",
    ) -> dict[str, Any]:
        """Make an authenticated GET request to the GitHub API."""
        url = f"{self._api_base}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code >= 400:
                    return {
                        "success": False,
                        "status_code": response.status_code,
                        "error": f"HTTP {response.status_code}",
                    }
                body = response.json() if response.content else {}
                return {"success": True, "status_code": response.status_code, "body": body}
            except httpx.HTTPError as exc:
                return {"success": False, "error": f"HTTP error: {exc}"}
            except Exception as exc:
                return {"success": False, "error": f"Unexpected error: {exc}"}


def recommended_workflow_timeout(time_budget_minutes: int) -> int:
    """Calculate the recommended workflow timeout given an engine time budget.

    Adds CI_TIMEOUT_BUFFER_MINUTES for setup/teardown (checkout, install deps,
    artifact upload, report generation).
    """
    return time_budget_minutes + CI_TIMEOUT_BUFFER_MINUTES
