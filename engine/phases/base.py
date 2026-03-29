"""Base phase class for pipeline phases."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from engine.config import EngineConfig
from engine.integrations.llm import LLMProvider
from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import ActionRecord, Tracer
from engine.observability.transcript import TranscriptWriter
from engine.phases.prompt_loader import load_prompt

if TYPE_CHECKING:
    from engine.tools.executor import ToolExecutor


TRIAGE_TOOLS: list[str] = ["file_read", "file_search", "shell_run"]
IMPLEMENT_TOOLS: list[str] = [
    "file_read",
    "file_write",
    "file_search",
    "shell_run",
    "git_diff",
    "git_commit",
]
REVIEW_TOOLS: list[str] = ["file_read", "file_search", "git_diff"]
VALIDATE_TOOLS: list[str] = [
    "file_read",
    "file_search",
    "shell_run",
    "git_diff",
    "github_api",
]
REPORT_TOOLS: list[str] = ["file_read", "file_search"]
CI_REMEDIATE_TOOLS: list[str] = [
    "file_read",
    "file_write",
    "file_search",
    "shell_run",
    "git_diff",
    "git_commit",
    "github_api",
]

PHASE_TOOL_SETS: dict[str, list[str]] = {
    "triage": TRIAGE_TOOLS,
    "implement": IMPLEMENT_TOOLS,
    "review": REVIEW_TOOLS,
    "validate": VALIDATE_TOOLS,
    "report": REPORT_TOOLS,
    "ci_remediate": CI_REMEDIATE_TOOLS,
}


@dataclass
class PhaseResult:
    """Result of a phase execution."""

    phase: str = ""
    success: bool = False
    should_continue: bool = True
    next_phase: str = ""
    escalate: bool = False
    escalation_reason: str = ""
    findings: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)


class Phase(ABC):
    """Base class for all pipeline phases.

    Each phase implements the OBSERVE -> PLAN -> ACT -> VALIDATE -> REFLECT cycle
    with phase-specific logic. Phases operate under zero trust: they re-read
    source material rather than trusting summaries from prior phases.

    Subclasses set ``name`` and optionally override ``allowed_tools`` (if not set,
    it falls back to the ``PHASE_TOOL_SETS`` mapping or allows all tools).
    """

    name: str = "base"
    allowed_tools: ClassVar[list[str]] = []

    def __init__(
        self,
        llm: LLMProvider,
        logger: StructuredLogger,
        tracer: Tracer,
        repo_path: str,
        issue_data: dict[str, Any],
        prior_phase_results: list[PhaseResult] | None = None,
        tool_executor: ToolExecutor | None = None,
        config: EngineConfig | None = None,
        metrics: LoopMetrics | None = None,
        transcript: TranscriptWriter | None = None,
    ):
        self.llm = llm
        self.logger = logger
        self.tracer = tracer
        self.repo_path = repo_path
        self.issue_data = issue_data
        self.prior_results = prior_phase_results or []
        self.tool_executor = tool_executor
        self.config = config or EngineConfig()
        self.metrics = metrics
        self.transcript = transcript

    @classmethod
    def get_allowed_tools(cls) -> list[str]:
        """Return the allowed tools for this phase.

        Priority: explicit ``allowed_tools`` ClassVar, then ``PHASE_TOOL_SETS``
        lookup by ``cls.name``, then empty list (all tools allowed).
        """
        if cls.allowed_tools:
            return list(cls.allowed_tools)
        return list(PHASE_TOOL_SETS.get(cls.name, []))

    def load_system_prompt(
        self,
        variables: dict[str, Any] | None = None,
        templates_dir: Path | None = None,
    ) -> str:
        """Load the system prompt template for this phase.

        Reads ``templates/prompts/{self.name}.md`` and renders with Jinja2 if
        ``variables`` are provided.
        """
        return load_prompt(self.name, variables=variables, templates_dir=templates_dir)

    def record_llm_call(
        self,
        description: str,
        model: str,
        provider: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: float,
        prompt_summary: str = "",
        response_summary: str = "",
        system_prompt: str = "",
        user_message: str = "",
        response: str = "",
    ) -> ActionRecord:
        """Record an LLM call in the tracer, metrics, and live HTML transcript.

        All phases should call this instead of ``self.tracer.record_llm_call()``
        directly so that ``LoopMetrics`` token/call counters stay in sync with
        the action trace and the transcript captures full inference context.
        """
        if self.metrics is not None:
            self.metrics.record_llm_call(tokens_in=tokens_in, tokens_out=tokens_out)

        if self.transcript is not None:
            self.transcript.record(
                phase=self.name,
                iteration=self.tracer._current_iteration,
                description=description,
                system_prompt=system_prompt,
                user_message=user_message,
                response=response,
                model=model,
                provider=provider,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
            )

        return self.tracer.record_llm_call(
            description=description,
            model=model,
            provider=provider,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            prompt_summary=prompt_summary,
            response_summary=response_summary,
        )

    @abstractmethod
    async def observe(self) -> dict[str, Any]:
        """Gather context for this phase. Re-read source material independently."""
        ...

    @abstractmethod
    async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Determine what actions to take based on observations."""
        ...

    @abstractmethod
    async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Execute the plan. Returns action results."""
        ...

    @abstractmethod
    async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
        """Verify the action achieved its goal."""
        ...

    @abstractmethod
    async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
        """Assess overall phase outcome. Decide whether to continue, iterate, or escalate."""
        ...

    async def execute(self) -> PhaseResult:
        """Run the full phase cycle: observe -> plan -> act -> validate -> reflect."""
        self.logger.set_phase(self.name)
        self.tracer.set_phase(self.name)
        self.logger.info(f"Starting phase: {self.name}")

        partial_context: dict[str, Any] = {}
        step = "init"
        try:
            step = "observe"
            observation = await self.observe()
            partial_context["observation_keys"] = (
                list(observation.keys())
                if isinstance(observation, dict)
                else str(type(observation))
            )

            step = "plan"
            plan = await self.plan(observation)
            partial_context["plan_keys"] = (
                list(plan.keys()) if isinstance(plan, dict) else str(type(plan))
            )

            step = "act"
            result = await self.act(plan)
            partial_context["act_keys"] = (
                list(result.keys()) if isinstance(result, dict) else str(type(result))
            )

            step = "validate"
            validation = await self.validate(result)
            partial_context["validate_keys"] = (
                list(validation.keys()) if isinstance(validation, dict) else str(type(validation))
            )

            step = "reflect"
            phase_result = await self.reflect(validation)
        except Exception as e:
            import traceback

            tb_str = traceback.format_exc()
            self.logger.error(f"Phase {self.name} failed at step '{step}': {e}")
            phase_result = PhaseResult(
                phase=self.name,
                success=False,
                should_continue=False,
                escalate=True,
                escalation_reason=f"Unhandled exception in {self.name} at step '{step}': {e}",
                findings={
                    "failed_step": step,
                    "partial_context": partial_context,
                    "traceback": tb_str[-1500:],
                },
            )

        self.logger.info(
            f"Phase {self.name} complete: success={phase_result.success}, "
            f"continue={phase_result.should_continue}, escalate={phase_result.escalate}"
        )
        return phase_result

    def _build_system_prompt(self, prompt_template: str, trusted_context: str = "") -> str:
        """Build a system prompt with proper trusted/untrusted separation."""
        return prompt_template + (f"\n\n{trusted_context}" if trusted_context else "")

    def _wrap_untrusted_content(self, content: str) -> str:
        """Wrap untrusted content (issue bodies, PR descriptions, etc.) with delimiters."""
        delimiter = self.config.security.untrusted_content_delimiter
        return f"{delimiter}\n\n{content}\n\n--- END UNTRUSTED CONTENT ---"
