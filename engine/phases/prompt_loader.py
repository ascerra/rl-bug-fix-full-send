"""Prompt template loading for Ralph Loop phases.

Loads markdown prompt templates from templates/prompts/ and renders them
with Jinja2 for variable substitution. Templates are cached after first load.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import jinja2

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "prompts"

_jinja_env: jinja2.Environment | None = None


def _get_jinja_env(templates_dir: Path | None = None) -> jinja2.Environment:
    global _jinja_env
    base = templates_dir or _TEMPLATES_DIR
    if _jinja_env is None or templates_dir is not None:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(base)),
            autoescape=False,
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
        )
        if templates_dir is not None:
            return env
        _jinja_env = env
    return _jinja_env


@lru_cache(maxsize=32)
def _read_raw(template_path: str) -> str:
    """Read a raw template file. Cached for repeated calls."""
    path = _TEMPLATES_DIR / template_path
    if not path.is_file():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text()


def load_prompt(
    phase_name: str,
    variables: dict[str, Any] | None = None,
    templates_dir: Path | None = None,
) -> str:
    """Load and render a phase prompt template.

    Args:
        phase_name: Name of the phase (maps to ``{phase_name}.md`` in the templates dir).
        variables: Optional Jinja2 template variables for rendering.
        templates_dir: Override the default templates directory (useful for testing).

    Returns:
        Rendered prompt string.

    Raises:
        FileNotFoundError: If the template file does not exist.
        jinja2.UndefinedError: If a required template variable is missing.
    """
    filename = f"{phase_name}.md"

    if templates_dir is not None:
        env = _get_jinja_env(templates_dir)
        template = env.get_template(filename)
        return template.render(**(variables or {}))

    if variables:
        env = _get_jinja_env()
        template = env.get_template(filename)
        return template.render(**variables)

    return _read_raw(filename)


def available_prompts(templates_dir: Path | None = None) -> list[str]:
    """Return a list of available phase prompt names (without .md extension)."""
    base = templates_dir or _TEMPLATES_DIR
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.md"))
