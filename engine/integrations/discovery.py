"""Integration discovery service — enumerate and probe available integrations.

Implements FR-4.7 (pluggable integration interface) and FR-4.8 (agent-driven
discovery). The DiscoveryService accepts registered IntegrationAdapter instances,
calls ``discover()`` on each to probe authentication and capabilities, and builds
a structured catalog that can be injected into LLM context for context gathering.

Auto-construction from config and secrets is provided by ``from_config()``.
"""

from __future__ import annotations

from typing import Any

from engine.config import EngineConfig, IntegrationsConfig
from engine.integrations import IntegrationAdapter
from engine.secrets import SecretManager

INTEGRATION_SECRET_REQUIREMENTS: dict[str, list[str]] = {
    "github": ["GH_PAT", "GITHUB_TOKEN"],
    "slack": ["SLACK_BOT_TOKEN"],
    "jira": ["JIRA_API_TOKEN"],
}


class DiscoveryService:
    """Enumerate available integrations and build a catalog for LLM context.

    Auto-detects configured integrations from:
    1. ``IntegrationsConfig`` — which integrations are enabled in YAML
    2. ``SecretManager`` — which secrets are present in the environment
    3. ``adapter.discover()`` — which APIs actually respond

    Adapters are registered via ``register_adapter()`` or auto-constructed
    via the ``from_config()`` classmethod.
    """

    def __init__(
        self,
        config: IntegrationsConfig | None = None,
        secrets: SecretManager | None = None,
    ):
        self._config = config or IntegrationsConfig()
        self._secrets = secrets or SecretManager()
        self._adapters: dict[str, IntegrationAdapter] = {}

    def register_adapter(self, adapter: IntegrationAdapter) -> None:
        """Register an integration adapter instance by its name."""
        self._adapters[adapter.name] = adapter

    def registered_adapters(self) -> list[str]:
        """Return sorted names of all registered adapters."""
        return sorted(self._adapters.keys())

    def has_required_secrets(self, integration_name: str) -> bool:
        """Check whether at least one required secret is set for an integration.

        Integrations with no known secret requirements (custom/unknown) return True.
        Integrations requiring ANY ONE of their listed secrets return True if at
        least one is available (e.g., github needs GH_PAT OR GITHUB_TOKEN).
        """
        required = INTEGRATION_SECRET_REQUIREMENTS.get(integration_name)
        if not required:
            return True
        return any(self._secrets.is_available(name) for name in required)

    def available_integrations(self) -> list[str]:
        """Return names of integrations that are enabled in config and have secrets.

        An integration is 'available' when:
        - It is enabled in the config (``enabled: true``)
        - At least one of its required secrets is present
        """
        available: list[str] = []
        config_map = {
            "github": self._config.github,
            "slack": self._config.slack,
            "jira": self._config.jira,
        }
        for name, cfg in config_map.items():
            if cfg.enabled and self.has_required_secrets(name):
                available.append(name)
        return sorted(available)

    async def discover_all(self) -> list[dict[str, Any]]:
        """Call ``discover()`` on every registered adapter and collect results.

        Each result dict includes the adapter's discovery output plus a
        ``_registered`` flag and the integration name.  Adapters that raise
        during discovery return an error entry rather than propagating.
        """
        results: list[dict[str, Any]] = []
        for name in sorted(self._adapters):
            adapter = self._adapters[name]
            try:
                discovery = await adapter.discover()
                discovery["_registered"] = True
                results.append(discovery)
            except Exception as exc:
                results.append(
                    {
                        "name": name,
                        "_registered": True,
                        "authenticated": False,
                        "error": f"Discovery failed: {exc}",
                    }
                )
        return results

    def build_catalog(
        self,
        discovery_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build a structured catalog suitable for LLM context injection.

        The catalog provides the LLM with a machine-readable summary of which
        external systems are available, their authentication status, and what
        capabilities they offer. This enables agent-driven context gathering
        (FR-4.8): the LLM can decide which integrations to query based on
        the catalog.

        Parameters
        ----------
        discovery_results:
            Output from ``discover_all()``. If None, the catalog is built from
            config/secrets availability alone (no API probing).
        """
        integrations: list[dict[str, Any]] = []
        all_available = self.available_integrations()

        if discovery_results:
            probed_names = {r.get("name", "") for r in discovery_results}
            for result in discovery_results:
                name = result.get("name", "unknown")
                integrations.append(
                    {
                        "name": name,
                        "enabled": name in all_available,
                        "authenticated": result.get("authenticated", False),
                        "capabilities": result.get("capabilities", []),
                        "error": result.get("error"),
                    }
                )
            for name in all_available:
                if name not in probed_names:
                    integrations.append(
                        {
                            "name": name,
                            "enabled": True,
                            "authenticated": None,
                            "capabilities": [],
                            "error": "Not probed (no adapter registered)",
                        }
                    )
        else:
            for name in all_available:
                integrations.append(
                    {
                        "name": name,
                        "enabled": True,
                        "authenticated": None,
                        "capabilities": [],
                        "error": None,
                    }
                )

        authenticated_count = sum(1 for i in integrations if i.get("authenticated") is True)
        return {
            "total_available": len(all_available),
            "total_authenticated": authenticated_count,
            "integrations": integrations,
        }

    def catalog_as_text(
        self,
        discovery_results: list[dict[str, Any]] | None = None,
    ) -> str:
        """Build a human/LLM-readable text summary of available integrations.

        Suitable for embedding directly in an LLM system prompt to inform the
        agent about available context sources.
        """
        catalog = self.build_catalog(discovery_results)
        lines = [
            f"Available integrations: {catalog['total_available']} "
            f"(authenticated: {catalog['total_authenticated']})",
            "",
        ]
        for integration in catalog["integrations"]:
            name = integration["name"]
            auth = integration["authenticated"]
            auth_str = "yes" if auth is True else ("no" if auth is False else "unknown")
            caps = ", ".join(integration.get("capabilities", [])) or "none listed"
            error = integration.get("error")

            lines.append(f"- {name}: authenticated={auth_str}, capabilities=[{caps}]")
            if error:
                lines.append(f"  error: {error}")

        return "\n".join(lines)

    @classmethod
    def from_config(
        cls,
        config: EngineConfig,
        secrets: SecretManager,
        issue_url: str = "",
    ) -> DiscoveryService:
        """Auto-construct adapters from config and secrets.

        Creates adapter instances for each enabled integration that has the
        required secrets available.  GitHub adapter construction requires an
        ``issue_url`` to derive owner/repo; it is skipped when the URL is not
        provided.
        """
        service = cls(config=config.integrations, secrets=secrets)

        if config.integrations.github.enabled and service.has_required_secrets("github"):
            try:
                from engine.integrations.github import GitHubAdapter

                token = secrets.get("GH_PAT") or secrets.get("GITHUB_TOKEN")
                if issue_url and "github.com/" in issue_url:
                    adapter = GitHubAdapter.from_issue_url(
                        issue_url,
                        token=token,
                        config=config.integrations.github,
                    )
                    service.register_adapter(adapter)
            except Exception:
                pass

        if config.integrations.slack.enabled and service.has_required_secrets("slack"):
            try:
                from engine.integrations.slack import SlackAdapter

                adapter = SlackAdapter(
                    token=secrets.get("SLACK_BOT_TOKEN"),
                    config=config.integrations.slack,
                )
                service.register_adapter(adapter)
            except Exception:
                pass

        if config.integrations.jira.enabled and service.has_required_secrets("jira"):
            try:
                from engine.integrations.jira import JiraAdapter

                adapter = JiraAdapter(
                    token=secrets.get("JIRA_API_TOKEN"),
                    email=secrets.get("JIRA_USER_EMAIL"),
                    config=config.integrations.jira,
                )
                service.register_adapter(adapter)
            except Exception:
                pass

        return service
