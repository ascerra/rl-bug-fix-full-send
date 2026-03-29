"""Narrative formatter — transforms raw action records into human-readable HTML fragments.

Produces HTML snippets for the 3D scene detail drill-down panel.  Every piece
of data passes through this formatter before display — the JS never parses
execution.json directly.  Code snippets use monospace formatting, diffs render
with colour, and LLM conversations are presented as readable narratives.
"""

from __future__ import annotations

import html
from typing import Any


class NarrativeFormatter:
    """Transforms raw action records into human-readable HTML fragments.

    Each method returns an HTML string using CSS classes from ``report.html``.
    The fragments are embedded in the scene-data JSON so the frontend
    ``detail-panel.js`` can render them directly without client-side parsing.
    """

    def format_action(self, action: dict[str, Any]) -> str:
        """Format any action record into a narrative HTML fragment."""
        action_type = action.get("action_type", "unknown")

        if action_type == "llm_query":
            return self.format_llm_call(action)
        if action_type in ("file_read", "file_write", "file_search"):
            return self.format_file_operation(action)
        if action_type in ("shell_run", "command_run", "tool_execution"):
            return self.format_command_run(action)
        if action_type in ("github_api", "api_call", "pr_create", "comment_post"):
            return self.format_api_call(action)
        if action_type == "escalation":
            return self.format_escalation(action)
        return self.format_generic(action)

    # ── LLM calls ────────────────────────────────────────────────────────

    def format_llm_call(self, action: dict[str, Any]) -> str:
        inp = action.get("input", {})
        output = action.get("output", {})
        llm_ctx = action.get("llm_context", {})
        provenance = action.get("provenance", {})
        description = inp.get("description", "No description available")

        parts: list[str] = []

        prompt_summary = self.summarize_prompt(
            inp.get("system_prompt", ""),
            description,
        )
        parts.append(
            '<div class="detail-section">'
            '<div class="detail-section-title">What the agent was told</div>'
            f'<p class="detail-narrative">{_esc(prompt_summary)}</p>'
            "</div>"
        )

        decision = _extract_decision(output, provenance)
        if decision:
            parts.append(
                '<div class="detail-section">'
                '<div class="detail-section-title">What it decided</div>'
                f'<p class="detail-narrative">{_esc(decision)}</p>'
                "</div>"
            )

        reasoning = self.extract_key_reasoning(provenance.get("reasoning", ""))
        if reasoning:
            parts.append(
                '<div class="detail-section">'
                '<div class="detail-section-title">Key reasoning</div>'
                f'<p class="detail-narrative">{_esc(reasoning)}</p>'
                "</div>"
            )

        kv_items: list[tuple[str, str]] = []
        model = llm_ctx.get("model", "")
        provider = llm_ctx.get("provider", "")
        tokens_in = llm_ctx.get("tokens_in", 0) or 0
        tokens_out = llm_ctx.get("tokens_out", 0) or 0
        duration = action.get("duration_ms", 0)
        if model:
            kv_items.append(("Model", _esc(model)))
        if provider:
            kv_items.append(("Provider", _esc(provider)))
        kv_items.append(("Tokens in", str(tokens_in)))
        kv_items.append(("Tokens out", str(tokens_out)))
        if duration:
            kv_items.append(("Response time", _format_ms(duration)))

        kv_html = "".join(
            f'<span class="kv-key">{k}</span><span class="kv-val">{v}</span>' for k, v in kv_items
        )
        parts.append(
            '<div class="detail-section">'
            '<div class="detail-section-title">By the numbers</div>'
            f'<div class="detail-kv-list">{kv_html}</div>'
            "</div>"
        )

        return "\n".join(parts)

    # ── File operations ──────────────────────────────────────────────────

    def format_file_operation(self, action: dict[str, Any]) -> str:
        inp = action.get("input", {})
        output = action.get("output", {})
        action_type = action.get("action_type", "file_read")
        provenance = action.get("provenance", {})

        file_path = inp.get("path", "") or (inp.get("context") or {}).get("path", "")
        description = inp.get("description", "")

        verb_map = {"file_write": "written", "file_read": "read", "file_search": "searched"}
        verb = verb_map.get(action_type, "accessed")

        parts: list[str] = []

        path_html = (
            f'<p><span class="detail-file-path">{_esc(file_path)}</span></p>' if file_path else ""
        )
        desc_html = f'<p class="detail-narrative">{_esc(description)}</p>' if description else ""
        parts.append(
            f'<div class="detail-section">'
            f'<div class="detail-section-title">What was {verb}</div>'
            f"{path_html}{desc_html}"
            f"</div>"
        )

        reasoning = provenance.get("reasoning", "")
        if reasoning:
            parts.append(
                '<div class="detail-section">'
                '<div class="detail-section-title">Why</div>'
                f'<p class="detail-narrative">{_esc(reasoning)}</p>'
                "</div>"
            )

        content = _extract_content(output)
        if content:
            truncated = content[:2000]
            suffix = "\n... (truncated)" if len(content) > 2000 else ""
            label = "What changed" if action_type == "file_write" else "Content (excerpt)"
            parts.append(
                f'<div class="detail-section">'
                f'<div class="detail-section-title">{label}</div>'
                f'<div class="detail-code-block">{_esc(truncated)}{_esc(suffix)}</div>'
                f"</div>"
            )

        return "\n".join(parts)

    # ── Command / shell runs ─────────────────────────────────────────────

    def format_command_run(self, action: dict[str, Any]) -> str:
        inp = action.get("input", {})
        output = action.get("output", {})
        provenance = action.get("provenance", {})

        description = inp.get("description", "No description")
        command = (
            inp.get("command", "") or (inp.get("context") or {}).get("command", "") or description
        )

        parts: list[str] = []

        parts.append(
            '<div class="detail-section">'
            '<div class="detail-section-title">What was run</div>'
            f'<div class="detail-code-block">{_esc(command)}</div>'
            "</div>"
        )

        success = output.get("success", False)
        out_data = output.get("data", output)
        stdout = ""
        stderr = ""
        if isinstance(out_data, dict):
            stdout = out_data.get("stdout", "") or ""
            stderr = out_data.get("stderr", "") or ""

        result_text = _summarize_command_result(success, stdout, stderr)
        if result_text:
            status_class = "status-success" if success else "status-failure"
            badge_text = "PASS" if success else "FAIL"
            parts.append(
                '<div class="detail-section">'
                '<div class="detail-section-title">What happened</div>'
                f'<span class="badge {status_class}"'
                ' style="margin-bottom:0.5rem;display:inline-block">'
                f"{badge_text}</span>"
                f'<div class="detail-code-block">{_esc(result_text)}</div>'
                "</div>"
            )

        reasoning = provenance.get("reasoning", "")
        if reasoning:
            parts.append(
                '<div class="detail-section">'
                '<div class="detail-section-title">What the agent did about it</div>'
                f'<p class="detail-narrative">{_esc(reasoning)}</p>'
                "</div>"
            )

        return "\n".join(parts)

    # ── API calls ────────────────────────────────────────────────────────

    def format_api_call(self, action: dict[str, Any]) -> str:
        inp = action.get("input", {})
        output = action.get("output", {})
        description = inp.get("description", "API call")

        parts: list[str] = []
        parts.append(
            '<div class="detail-section">'
            '<div class="detail-section-title">What was requested</div>'
            f'<p class="detail-narrative">{_esc(description)}</p>'
            "</div>"
        )

        success = output.get("success", False)
        status_class = "status-success" if success else "status-failure"
        error = output.get("error", "")
        result_msg = "Request completed successfully." if success else "Request failed."
        if error:
            result_msg += f" {str(error)[:200]}"

        parts.append(
            '<div class="detail-section">'
            '<div class="detail-section-title">Result</div>'
            f'<span class="badge {status_class}" style="display:inline-block">'
            f"{'SUCCESS' if success else 'FAIL'}</span> "
            f'<span class="detail-narrative">{_esc(result_msg)}</span>'
            "</div>"
        )

        return "\n".join(parts)

    # ── Escalation ───────────────────────────────────────────────────────

    def format_escalation(self, action: dict[str, Any]) -> str:
        inp = action.get("input", {})
        description = inp.get("description", "")
        reason = inp.get("reason", "") or description

        return (
            '<div class="detail-section">'
            '<div class="detail-section-title">Escalation</div>'
            '<p class="detail-narrative" style="color:var(--escalated)">'
            "The agent determined it could not proceed autonomously and escalated "
            "this issue for human review.</p>"
            f'<p class="detail-narrative">{_esc(reason)}</p>'
            "</div>"
        )

    # ── Generic fallback ─────────────────────────────────────────────────

    def format_generic(self, action: dict[str, Any]) -> str:
        inp = action.get("input", {})
        output = action.get("output", {})
        description = inp.get("description", "Action performed")
        success = output.get("success")

        parts: list[str] = []
        parts.append(
            '<div class="detail-section">'
            '<div class="detail-section-title">Action</div>'
            f'<p class="detail-narrative">{_esc(description)}</p>'
            "</div>"
        )

        if success is not None:
            status_class = "status-success" if success else "status-failure"
            badge = "SUCCESS" if success else "FAIL"
            parts.append(
                '<div class="detail-section">'
                f'<span class="badge {status_class}" style="display:inline-block">{badge}</span>'
                "</div>"
            )

        return "\n".join(parts)

    # ── Phase transitions ────────────────────────────────────────────────

    def format_phase_transition(self, reflection: dict[str, Any]) -> str:
        """Format a phase transition reflection into narrative HTML."""
        next_phase = reflection.get("next_phase", "")
        success = reflection.get("success", False)
        reason = reflection.get("reasoning", "") or reflection.get("escalation_reason", "")
        carried = reflection.get("carried_forward", "") or reflection.get("summary", "")

        parts: list[str] = []

        if success:
            why = "Phase completed successfully."
            if reason:
                why += f" {reason}"
        elif reflection.get("escalate"):
            why = "The agent escalated to human review."
            if reason:
                why += f" {reason}"
        else:
            why = reason or "Phase did not complete successfully."

        if next_phase:
            why += f" Advancing to the {next_phase} phase."

        parts.append(
            '<div class="detail-section">'
            '<div class="detail-section-title">Why did the agent move on?</div>'
            f'<p class="detail-narrative">{_esc(why)}</p>'
            "</div>"
        )

        if carried:
            parts.append(
                '<div class="detail-section">'
                '<div class="detail-section-title">What was carried forward?</div>'
                f'<p class="detail-narrative">{_esc(carried)}</p>'
                "</div>"
            )

        return "\n".join(parts)

    # ── Prompt summarisation ─────────────────────────────────────────────

    def summarize_prompt(self, system_prompt: str, context: str = "") -> str:
        """Transform a raw system prompt into a 1-2 sentence summary."""
        if not system_prompt and not context:
            return context or "No prompt information available."

        if context and not system_prompt:
            return context

        prompt = system_prompt.strip().lower()

        phase_tasks = {
            "triage": "classify the issue as a bug, feature request, or ambiguous, "
            "and identify affected components",
            "implement": "analyze the code and implement a fix for the identified bug",
            "review": "independently review the proposed fix for correctness, "
            "security, and scope compliance",
            "validate": "run final checks and prepare the pull request",
            "ci": "analyze CI failure details and implement a targeted fix",
            "report": "generate the execution report and visual evidence",
        }

        task = "process the given context and produce a structured response"
        for keyword, description in phase_tasks.items():
            if keyword in prompt:
                task = description
                break

        summary = f"The agent was asked to {task}"
        if context:
            summary += f" \u2014 specifically: {_truncate(context, 200)}"
        else:
            summary += "."

        return summary

    # ── Reasoning extraction ─────────────────────────────────────────────

    def extract_key_reasoning(self, reasoning: str) -> str:
        """Extract key reasoning and format as readable text."""
        if not reasoning:
            return ""
        text = reasoning.strip()
        if len(text) > 2000:
            text = text[:2000] + "..."
        return text


# ── Scene enrichment ─────────────────────────────────────────────────────


def enrich_scene_with_narratives(
    scene_dict: dict[str, Any],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add ``narrative_html`` to each scene object from raw action records.

    Mutates *scene_dict* in-place and returns it for convenience.  The JS
    detail panel renders these pre-formatted fragments instead of parsing
    raw JSON client-side.
    """
    formatter = NarrativeFormatter()
    actions_by_id: dict[str, dict[str, Any]] = {a.get("id", ""): a for a in actions if a.get("id")}

    for platform in scene_dict.get("platforms", []):
        for obj in platform.get("objects", []):
            obj_id = obj.get("id", "")
            action = actions_by_id.get(obj_id)
            if action:
                obj.setdefault("meta", {})["narrative_html"] = formatter.format_action(action)

    return scene_dict


# ── Helpers ──────────────────────────────────────────────────────────────


def _esc(text: str | Any) -> str:
    """HTML-escape a string."""
    return html.escape(str(text)) if text else ""


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _format_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60
    return f"{minutes:.1f}m"


def _extract_decision(output: dict[str, Any], provenance: dict[str, Any]) -> str:
    """Extract a human-readable decision summary from output/provenance."""
    if provenance.get("decision"):
        return str(provenance["decision"])
    if output.get("verdict"):
        return f"Verdict: {output['verdict']}"
    if output.get("classification"):
        return f"Classification: {output['classification']}"
    if output.get("summary"):
        return str(output["summary"])
    return ""


def _extract_content(output: dict[str, Any]) -> str:
    """Extract text content from an action output dict."""
    out_data = output.get("data", output)
    if isinstance(out_data, dict):
        return out_data.get("content", "") or out_data.get("stdout", "") or ""
    if isinstance(out_data, str):
        return out_data
    return ""


def _summarize_command_result(success: bool, stdout: str, stderr: str) -> str:
    """Keep only the relevant portion of command output."""
    if success and stdout:
        lines = stdout.strip().splitlines()
        for line in reversed(lines):
            stripped = line.strip()
            if any(kw in stripped.lower() for kw in ("passed", "ok", "success", "tests run")):
                return stripped
        if len(lines) <= 5:
            return stdout.strip()[:1500]
        return "\n".join(lines[-5:])[:1500]

    relevant = stderr.strip() if stderr else stdout.strip()
    if not relevant:
        return "No output captured." if not success else ""

    lines = relevant.splitlines()
    if len(lines) > 20:
        return "\n".join(lines[-20:])[:1500]
    return relevant[:1500]
