"""Live LLM transcript writer — real-time visibility into every LLM call.

Writes a self-contained HTML file that accumulates during the run.  Each LLM
call appears as a collapsible section with phase badge, timing, token counts,
the full system prompt, user message, and raw LLM response.

The transcript file is continuously appended so it can be viewed mid-run
(download the artifact or tail the file) and is uploaded alongside other
execution artifacts.
"""

from __future__ import annotations

import html
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine.secrets import SecretRedactor

_CALL_COUNTER = 0

_HTML_HEADER = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ralph Loop — LLM Transcript</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --orange: #d29922; --red: #f85149;
    --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
    padding: 24px; max-width: 1200px; margin: 0 auto;
  }
  h1 { color: var(--accent); margin-bottom: 8px; font-size: 1.5rem; }
  .subtitle { color: var(--muted); margin-bottom: 24px; font-size: 0.9rem; }
  .call {
    border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 16px; background: var(--surface); overflow: hidden;
  }
  .call-header {
    display: flex; align-items: center; gap: 10px; padding: 12px 16px;
    cursor: pointer; user-select: none; flex-wrap: wrap;
  }
  .call-header:hover { background: #1c2128; }
  .call-num {
    background: var(--accent); color: #0d1117; border-radius: 4px;
    padding: 2px 8px; font-weight: 700; font-size: 0.8rem; min-width: 28px;
    text-align: center;
  }
  .phase-badge {
    border-radius: 4px; padding: 2px 8px; font-size: 0.75rem;
    font-weight: 600; text-transform: uppercase;
  }
  .phase-triage { background: #1f3a5f; color: var(--accent); }
  .phase-implement { background: #1a3a1a; color: var(--green); }
  .phase-review { background: #3a2a0a; color: var(--orange); }
  .phase-validate { background: #2a1a3a; color: var(--purple); }
  .phase-report { background: #1a2a2a; color: #56d4dd; }
  .phase-init { background: #2a2a2a; color: var(--muted); }
  .call-desc { flex: 1; font-weight: 500; }
  .call-meta { color: var(--muted); font-size: 0.8rem; white-space: nowrap; }
  .call-body { display: none; border-top: 1px solid var(--border); }
  .call.open .call-body { display: block; }
  .section { padding: 12px 16px; border-bottom: 1px solid var(--border); }
  .section:last-child { border-bottom: none; }
  .section-label {
    color: var(--muted); font-size: 0.75rem; text-transform: uppercase;
    letter-spacing: 0.05em; margin-bottom: 6px; font-weight: 600;
  }
  .section pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px; overflow-x: auto; font-size: 0.82rem; line-height: 1.5;
    white-space: pre-wrap; word-break: break-word; color: var(--text);
    max-height: 600px; overflow-y: auto;
  }
  .arrow { transition: transform 0.15s; display: inline-block; color: var(--muted); }
  .call.open .arrow { transform: rotate(90deg); }
  .token-pill {
    background: var(--bg); border: 1px solid var(--border); border-radius: 12px;
    padding: 1px 8px; font-size: 0.75rem; color: var(--muted);
  }
  #auto-refresh { position: fixed; bottom: 16px; right: 16px; z-index: 10;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 14px; color: var(--muted); font-size: 0.8rem; cursor: pointer; }
  #auto-refresh:hover { border-color: var(--accent); color: var(--accent); }
  #auto-refresh.active { border-color: var(--green); color: var(--green); }
</style>
</head>
<body>
<h1>LLM Transcript</h1>
<div class="subtitle">Ralph Loop — live inference log</div>
<div id="calls">
"""

_HTML_FOOTER = """\
</div>
<button id="auto-refresh" onclick="toggleRefresh()">&#x21bb; Auto-refresh: OFF</button>
<script>
document.addEventListener('click', e => {
  const hdr = e.target.closest('.call-header');
  if (hdr) hdr.parentElement.classList.toggle('open');
});
let _interval = null;
function toggleRefresh() {
  const btn = document.getElementById('auto-refresh');
  if (_interval) { clearInterval(_interval); _interval = null;
    btn.classList.remove('active'); btn.innerHTML = '&#x21bb; Auto-refresh: OFF';
  } else { _interval = setInterval(() => location.reload(), 5000);
    btn.classList.add('active'); btn.innerHTML = '&#x21bb; Auto-refresh: 5s';
  }
}
</script>
</body>
</html>
"""


def _phase_css_class(phase: str) -> str:
    known = ("triage", "implement", "review", "validate", "report")
    return f"phase-{phase}" if phase in known else "phase-init"


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _truncate(text: str, limit: int = 200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


class TranscriptWriter:
    """Accumulates LLM call records and writes a live HTML transcript."""

    def __init__(
        self,
        output_path: str | Path | None = None,
        redactor: SecretRedactor | None = None,
    ):
        self._path = Path(output_path) if output_path else None
        self._redactor = redactor
        self._calls: list[dict[str, Any]] = []
        self._file_started = False

    def record(
        self,
        *,
        phase: str,
        iteration: int,
        description: str,
        system_prompt: str = "",
        user_message: str = "",
        response: str = "",
        model: str = "",
        provider: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
    ) -> dict[str, Any]:
        """Record an LLM call and append it to the HTML transcript file."""
        if self._redactor:
            system_prompt = self._redactor.redact(system_prompt)
            user_message = self._redactor.redact(user_message)
            response = self._redactor.redact(response)
            description = self._redactor.redact(description)

        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "phase": phase,
            "iteration": iteration,
            "description": description,
            "system_prompt": system_prompt,
            "user_message": user_message,
            "response": response,
            "model": model,
            "provider": provider,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
        }
        self._calls.append(entry)

        self._append_to_html(entry)
        self._print_inline(entry)
        return entry

    def get_calls(self) -> list[dict[str, Any]]:
        return list(self._calls)

    def _print_inline(self, entry: dict[str, Any]) -> None:
        """Print LLM call details to stderr for live CI job log visibility."""
        phase = entry["phase"].upper()
        desc = entry["description"]
        model = entry["model"]
        tokens = f"{entry['tokens_in']}→{entry['tokens_out']} tok"
        latency = f"{entry['latency_ms']:.0f}ms"
        response = entry["response"]

        print(
            f"\n{'=' * 80}\n"
            f">>> [{phase}] LLM CALL: {desc}\n"
            f"    Model: {model} | {tokens} | {latency}\n"
            f"{'=' * 80}",
            file=sys.stderr,
        )

        sys_prompt = entry.get("system_prompt", "")
        if sys_prompt:
            print(
                f"--- SYSTEM PROMPT ({len(sys_prompt)} chars) ---\n{_truncate(sys_prompt, 1000)}\n",
                file=sys.stderr,
            )

        user_msg = entry.get("user_message", "")
        if user_msg:
            print(
                f"--- USER MESSAGE ({len(user_msg)} chars) ---\n{_truncate(user_msg, 2000)}\n",
                file=sys.stderr,
            )

        print(
            f"--- LLM RESPONSE ({len(response)} chars) ---\n"
            f"{_truncate(response, 3000)}\n"
            f"{'=' * 80}\n",
            file=sys.stderr,
        )

    def _append_to_html(self, entry: dict[str, Any]) -> None:
        """Append a single call section to the HTML file.

        On the first call, writes the header.  On each subsequent call, seeks
        back before the footer and appends the new section + footer.  This
        keeps the file always-valid HTML so a browser can render it mid-run.
        """
        if not self._path:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)

        call_num = len(self._calls)
        section_html = self._render_call(entry, call_num)

        if not self._file_started:
            with self._path.open("w") as f:
                f.write(_HTML_HEADER)
                f.write(section_html)
                f.write(_HTML_FOOTER)
            self._file_started = True
        else:
            content = self._path.read_text()
            marker = "</div>\n<button"
            idx = content.rfind(marker)
            if idx == -1:
                with self._path.open("a") as f:
                    f.write(section_html)
            else:
                with self._path.open("w") as f:
                    f.write(content[:idx])
                    f.write(section_html)
                    f.write(content[idx:])

    def _render_call(self, entry: dict[str, Any], call_num: int) -> str:
        phase = entry["phase"]
        phase_cls = _phase_css_class(phase)
        desc = _esc(entry["description"])
        model = _esc(entry["model"])
        _esc(entry["provider"])
        ts = entry["timestamp"][:19].replace("T", " ")
        tokens_in = entry["tokens_in"]
        tokens_out = entry["tokens_out"]
        latency = entry["latency_ms"]
        system_prompt = _esc(entry["system_prompt"])
        user_message = _esc(entry["user_message"])
        response = _esc(entry["response"])

        return textwrap.dedent(f"""\
        <div class="call">
          <div class="call-header">
            <span class="arrow">▶</span>
            <span class="call-num">#{call_num}</span>
            <span class="phase-badge {phase_cls}">{_esc(phase)}</span>
            <span class="call-desc">{desc}</span>
            <span class="token-pill">{tokens_in}↦{tokens_out} tok</span>
            <span class="call-meta">{model} · {latency:.0f}ms · {ts}</span>
          </div>
          <div class="call-body">
            <div class="section">
              <div class="section-label">System Prompt</div>
              <pre>{system_prompt}</pre>
            </div>
            <div class="section">
              <div class="section-label">User Message</div>
              <pre>{user_message}</pre>
            </div>
            <div class="section">
              <div class="section-label">LLM Response</div>
              <pre>{response}</pre>
            </div>
          </div>
        </div>
        """)

    def finalize(self) -> None:
        """Inject a summary stats section at the end of the transcript HTML."""
        if not self._path or not self._calls:
            return

        total_calls = len(self._calls)
        total_tokens_in = sum(c.get("tokens_in", 0) for c in self._calls)
        total_tokens_out = sum(c.get("tokens_out", 0) for c in self._calls)
        total_latency = sum(c.get("latency_ms", 0) for c in self._calls)

        phase_counts: dict[str, int] = {}
        for c in self._calls:
            p = c.get("phase", "unknown")
            phase_counts[p] = phase_counts.get(p, 0) + 1

        phase_rows = "".join(
            f"<tr><td>{_esc(p)}</td><td>{n}</td></tr>" for p, n in phase_counts.items()
        )

        summary_html = textwrap.dedent(f"""\
        <div class="call" style="border-color: var(--accent);">
          <div class="call-header" style="background: #0d2137;">
            <span class="call-num" style="background: var(--green);">&#x2211;</span>
            <span class="call-desc" style="color: var(--green);">Transcript Summary</span>
          </div>
          <div class="call-body" style="display: block;">
            <div class="section">
              <div class="section-label">Totals</div>
              <pre>LLM calls:     {total_calls}
Tokens in:     {total_tokens_in:,}
Tokens out:    {total_tokens_out:,}
Total latency: {total_latency:,.0f}ms ({total_latency / 1000:.1f}s)</pre>
            </div>
            <div class="section">
              <div class="section-label">Calls per Phase</div>
              <table style="color: var(--text); font-size: 0.85rem;">
                <tr><th style="text-align:left; padding-right:16px;">Phase</th><th>Calls</th></tr>
                {phase_rows}
              </table>
            </div>
          </div>
        </div>
        """)

        try:
            content = self._path.read_text()
            marker = "</div>\n<button"
            idx = content.rfind(marker)
            if idx != -1:
                with self._path.open("w") as f:
                    f.write(content[:idx])
                    f.write(summary_html)
                    f.write(content[idx:])
        except Exception:
            pass
