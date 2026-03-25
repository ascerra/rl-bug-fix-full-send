"""Secret management — reads secrets from environment, provides redaction.

Secrets are read from environment variables and never logged. The SecretRedactor
scrubs known secret values from any string before it reaches logs, traces, or
tool output. This module is intentionally dependency-free within the engine to
avoid circular imports.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

KNOWN_SECRET_ENV_VARS: dict[str, str] = {
    "GEMINI_API_KEY": "LLM access (Google Gemini)",
    "ANTHROPIC_API_KEY": "LLM access (Anthropic Claude)",
    "GH_PAT": "GitHub API (Personal Access Token with repo scope)",
    "GITHUB_TOKEN": "GitHub API (Actions-provided or PAT)",
    "SLACK_BOT_TOKEN": "Slack API (Bot token for notifications and channel reading)",
    "JIRA_API_TOKEN": "Jira API (API token for Cloud or PAT for Data Center)",
    "JIRA_USER_EMAIL": "Jira API (User email for Cloud basic auth)",
}

PROVIDER_REQUIRED_SECRETS: dict[str, list[str]] = {
    "gemini": ["GEMINI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "mock": [],
}

REDACTED_PLACEHOLDER = "***REDACTED:{name}***"
MIN_SECRET_LENGTH = 4


class SecretRedactor:
    """Replaces known secret values with masked placeholders in arbitrary strings.

    Secrets shorter than MIN_SECRET_LENGTH are ignored to avoid false-positive
    redaction of common substrings (e.g., a 1-char token would match everywhere).
    """

    def __init__(self, secrets: dict[str, str]):
        self._replacements: list[tuple[re.Pattern[str], str]] = []
        for name, value in secrets.items():
            if value and len(value) >= MIN_SECRET_LENGTH:
                pattern = re.compile(re.escape(value))
                placeholder = REDACTED_PLACEHOLDER.format(name=name)
                self._replacements.append((pattern, placeholder))

    def redact(self, text: str) -> str:
        """Replace all known secret values in *text* with redacted placeholders."""
        for pattern, placeholder in self._replacements:
            text = pattern.sub(placeholder, text)
        return text

    def redact_value(self, value: Any) -> Any:
        """Redact if the value is a string, otherwise return unchanged."""
        if isinstance(value, str):
            return self.redact(value)
        return value

    def redact_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """Deep-redact all string values in a dict (one level of nesting)."""
        redacted: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, str):
                redacted[k] = self.redact(v)
            elif isinstance(v, dict):
                redacted[k] = self.redact_dict(v)
            elif isinstance(v, list):
                redacted[k] = [self.redact(item) if isinstance(item, str) else item for item in v]
            else:
                redacted[k] = v
        return redacted


_NOOP_REDACTOR: SecretRedactor | None = None


def noop_redactor() -> SecretRedactor:
    """Return a no-op redactor (no secrets registered). Used in tests."""
    global _NOOP_REDACTOR
    if _NOOP_REDACTOR is None:
        _NOOP_REDACTOR = SecretRedactor({})
    return _NOOP_REDACTOR


@dataclass
class SecretManager:
    """Manages secrets loaded from environment variables.

    Provides:
    - Discovery of available secrets (names only, never values in output)
    - Validation that required secrets are present
    - A SecretRedactor that scrubs secret values from strings
    """

    _secrets: dict[str, str] = field(default_factory=dict, repr=False)
    _redactor: SecretRedactor | None = field(default=None, repr=False)

    @classmethod
    def from_environment(cls) -> SecretManager:
        """Load all known secrets from environment variables."""
        secrets: dict[str, str] = {}
        for env_var in KNOWN_SECRET_ENV_VARS:
            value = os.environ.get(env_var, "")
            if value:
                secrets[env_var] = value
        return cls(_secrets=secrets)

    def get(self, name: str) -> str | None:
        """Get a secret value by name. Returns None if not set."""
        return self._secrets.get(name) or None

    def available(self) -> list[str]:
        """List names of secrets that are set (never exposes values)."""
        return sorted(self._secrets.keys())

    def is_available(self, name: str) -> bool:
        """Check whether a specific secret is set."""
        return bool(self._secrets.get(name))

    def validate_for_provider(self, provider: str) -> list[str]:
        """Check that all required secrets for a provider are set.

        Returns a list of missing secret names. Empty list means all present.
        """
        required = PROVIDER_REQUIRED_SECRETS.get(provider, [])
        return [name for name in required if not self.is_available(name)]

    def require_for_provider(self, provider: str) -> None:
        """Raise RuntimeError if any required secrets for the provider are missing."""
        missing = self.validate_for_provider(provider)
        if missing:
            descriptions = [
                f"  {name}: {KNOWN_SECRET_ENV_VARS.get(name, 'unknown')}" for name in missing
            ]
            raise RuntimeError(
                f"Missing required secrets for provider '{provider}':\n"
                + "\n".join(descriptions)
                + "\n\nSet them as environment variables before running the engine."
            )

    @property
    def redactor(self) -> SecretRedactor:
        """Get a redactor configured with all loaded secrets."""
        if self._redactor is None:
            self._redactor = SecretRedactor(self._secrets)
        return self._redactor
