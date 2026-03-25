"""Tests for engine.integrations.discovery — DiscoveryService."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from engine.config import (
    EngineConfig,
    GitHubIntegrationConfig,
    IntegrationsConfig,
    JiraIntegrationConfig,
    SlackIntegrationConfig,
)
from engine.integrations import IntegrationAdapter
from engine.integrations.discovery import (
    INTEGRATION_SECRET_REQUIREMENTS,
    DiscoveryService,
)
from engine.secrets import SecretManager

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeAdapter:
    """Minimal IntegrationAdapter implementation for testing."""

    def __init__(
        self,
        name: str = "fake",
        discover_result: dict[str, Any] | None = None,
        discover_raises: Exception | None = None,
    ):
        self.name = name
        self._result = discover_result or {
            "name": name,
            "authenticated": True,
            "capabilities": ["read", "write"],
        }
        self._raises = discover_raises

    async def discover(self) -> dict[str, Any]:
        if self._raises:
            raise self._raises
        return dict(self._result)

    async def read(self, resource_id: str) -> dict[str, Any]:
        return {"success": True}

    async def write(self, resource_id: str, content: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    async def search(self, query: str) -> list[dict[str, Any]]:
        return []


def _make_secrets(**env_vars: str) -> SecretManager:
    """Create a SecretManager with specific env vars set."""
    with patch.dict("os.environ", env_vars, clear=True):
        return SecretManager.from_environment()


def _make_config(
    github_enabled: bool = True,
    slack_enabled: bool = False,
    jira_enabled: bool = False,
    jira_server_url: str = "",
) -> IntegrationsConfig:
    return IntegrationsConfig(
        github=GitHubIntegrationConfig(enabled=github_enabled),
        slack=SlackIntegrationConfig(enabled=slack_enabled),
        jira=JiraIntegrationConfig(enabled=jira_enabled, server_url=jira_server_url),
    )


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_fake_adapter_satisfies_protocol(self):
        adapter = FakeAdapter()
        assert isinstance(adapter, IntegrationAdapter)

    def test_secret_requirements_has_known_integrations(self):
        assert "github" in INTEGRATION_SECRET_REQUIREMENTS
        assert "slack" in INTEGRATION_SECRET_REQUIREMENTS
        assert "jira" in INTEGRATION_SECRET_REQUIREMENTS


# ---------------------------------------------------------------------------
# Constructor and registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_default_constructor(self):
        service = DiscoveryService()
        assert service.registered_adapters() == []

    def test_register_adapter(self):
        service = DiscoveryService()
        service.register_adapter(FakeAdapter(name="github"))
        assert service.registered_adapters() == ["github"]

    def test_register_multiple_adapters(self):
        service = DiscoveryService()
        service.register_adapter(FakeAdapter(name="github"))
        service.register_adapter(FakeAdapter(name="slack"))
        service.register_adapter(FakeAdapter(name="jira"))
        assert service.registered_adapters() == ["github", "jira", "slack"]

    def test_register_overwrites_same_name(self):
        service = DiscoveryService()
        adapter1 = FakeAdapter(name="github", discover_result={"name": "github", "v": 1})
        adapter2 = FakeAdapter(name="github", discover_result={"name": "github", "v": 2})
        service.register_adapter(adapter1)
        service.register_adapter(adapter2)
        assert service.registered_adapters() == ["github"]
        assert service._adapters["github"] is adapter2


# ---------------------------------------------------------------------------
# has_required_secrets
# ---------------------------------------------------------------------------


class TestHasRequiredSecrets:
    def test_github_with_gh_pat(self):
        secrets = _make_secrets(GH_PAT="token123")
        service = DiscoveryService(secrets=secrets)
        assert service.has_required_secrets("github") is True

    def test_github_with_github_token(self):
        secrets = _make_secrets(GITHUB_TOKEN="token456")
        service = DiscoveryService(secrets=secrets)
        assert service.has_required_secrets("github") is True

    def test_github_no_token(self):
        secrets = _make_secrets()
        service = DiscoveryService(secrets=secrets)
        assert service.has_required_secrets("github") is False

    def test_slack_with_token(self):
        secrets = _make_secrets(SLACK_BOT_TOKEN="xoxb-123")
        service = DiscoveryService(secrets=secrets)
        assert service.has_required_secrets("slack") is True

    def test_slack_no_token(self):
        secrets = _make_secrets()
        service = DiscoveryService(secrets=secrets)
        assert service.has_required_secrets("slack") is False

    def test_jira_with_token(self):
        secrets = _make_secrets(JIRA_API_TOKEN="jira-tok")
        service = DiscoveryService(secrets=secrets)
        assert service.has_required_secrets("jira") is True

    def test_jira_no_token(self):
        secrets = _make_secrets()
        service = DiscoveryService(secrets=secrets)
        assert service.has_required_secrets("jira") is False

    def test_unknown_integration_returns_true(self):
        secrets = _make_secrets()
        service = DiscoveryService(secrets=secrets)
        assert service.has_required_secrets("custom_thing") is True


# ---------------------------------------------------------------------------
# available_integrations
# ---------------------------------------------------------------------------


class TestAvailableIntegrations:
    def test_github_enabled_with_secret(self):
        config = _make_config(github_enabled=True)
        secrets = _make_secrets(GH_PAT="tok")
        service = DiscoveryService(config=config, secrets=secrets)
        assert "github" in service.available_integrations()

    def test_github_disabled_not_available(self):
        config = _make_config(github_enabled=False)
        secrets = _make_secrets(GH_PAT="tok")
        service = DiscoveryService(config=config, secrets=secrets)
        assert "github" not in service.available_integrations()

    def test_github_enabled_no_secret(self):
        config = _make_config(github_enabled=True)
        secrets = _make_secrets()
        service = DiscoveryService(config=config, secrets=secrets)
        assert "github" not in service.available_integrations()

    def test_all_enabled_with_secrets(self):
        config = _make_config(
            github_enabled=True,
            slack_enabled=True,
            jira_enabled=True,
        )
        secrets = _make_secrets(
            GH_PAT="tok",
            SLACK_BOT_TOKEN="xoxb",
            JIRA_API_TOKEN="jira",
        )
        service = DiscoveryService(config=config, secrets=secrets)
        available = service.available_integrations()
        assert available == ["github", "jira", "slack"]

    def test_none_enabled(self):
        config = _make_config(
            github_enabled=False,
            slack_enabled=False,
            jira_enabled=False,
        )
        secrets = _make_secrets(GH_PAT="tok", SLACK_BOT_TOKEN="xoxb")
        service = DiscoveryService(config=config, secrets=secrets)
        assert service.available_integrations() == []

    def test_default_config_github_only(self):
        """Default config has github=True, slack=False, jira=False."""
        secrets = _make_secrets(GH_PAT="tok")
        service = DiscoveryService(secrets=secrets)
        assert service.available_integrations() == ["github"]


# ---------------------------------------------------------------------------
# discover_all
# ---------------------------------------------------------------------------


class TestDiscoverAll:
    @pytest.mark.asyncio
    async def test_empty_service(self):
        service = DiscoveryService()
        results = await service.discover_all()
        assert results == []

    @pytest.mark.asyncio
    async def test_single_adapter_success(self):
        service = DiscoveryService()
        service.register_adapter(
            FakeAdapter(
                name="github",
                discover_result={
                    "name": "github",
                    "authenticated": True,
                    "capabilities": ["read_issue"],
                },
            )
        )
        results = await service.discover_all()
        assert len(results) == 1
        assert results[0]["name"] == "github"
        assert results[0]["authenticated"] is True
        assert results[0]["_registered"] is True

    @pytest.mark.asyncio
    async def test_multiple_adapters(self):
        service = DiscoveryService()
        service.register_adapter(FakeAdapter(name="github"))
        service.register_adapter(FakeAdapter(name="slack"))
        results = await service.discover_all()
        assert len(results) == 2
        names = [r["name"] for r in results]
        assert "github" in names
        assert "slack" in names

    @pytest.mark.asyncio
    async def test_adapter_discover_raises(self):
        service = DiscoveryService()
        service.register_adapter(
            FakeAdapter(
                name="broken",
                discover_raises=RuntimeError("connection refused"),
            )
        )
        results = await service.discover_all()
        assert len(results) == 1
        assert results[0]["name"] == "broken"
        assert results[0]["authenticated"] is False
        assert "connection refused" in results[0]["error"]
        assert results[0]["_registered"] is True

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self):
        service = DiscoveryService()
        service.register_adapter(
            FakeAdapter(
                name="github",
                discover_result={
                    "name": "github",
                    "authenticated": True,
                    "capabilities": ["read_issue"],
                },
            )
        )
        service.register_adapter(
            FakeAdapter(
                name="slack",
                discover_raises=ConnectionError("timeout"),
            )
        )
        results = await service.discover_all()
        assert len(results) == 2
        github_result = next(r for r in results if r["name"] == "github")
        slack_result = next(r for r in results if r["name"] == "slack")
        assert github_result["authenticated"] is True
        assert slack_result["authenticated"] is False

    @pytest.mark.asyncio
    async def test_results_sorted_by_name(self):
        service = DiscoveryService()
        service.register_adapter(FakeAdapter(name="slack"))
        service.register_adapter(FakeAdapter(name="github"))
        service.register_adapter(FakeAdapter(name="jira"))
        results = await service.discover_all()
        names = [r["name"] for r in results]
        assert names == ["github", "jira", "slack"]


# ---------------------------------------------------------------------------
# build_catalog
# ---------------------------------------------------------------------------


class TestBuildCatalog:
    def test_catalog_without_discovery(self):
        config = _make_config(github_enabled=True, slack_enabled=True)
        secrets = _make_secrets(GH_PAT="tok", SLACK_BOT_TOKEN="xoxb")
        service = DiscoveryService(config=config, secrets=secrets)

        catalog = service.build_catalog()
        assert catalog["total_available"] == 2
        assert catalog["total_authenticated"] == 0
        assert len(catalog["integrations"]) == 2
        names = [i["name"] for i in catalog["integrations"]]
        assert "github" in names
        assert "slack" in names
        for entry in catalog["integrations"]:
            assert entry["enabled"] is True
            assert entry["authenticated"] is None

    def test_catalog_with_discovery_results(self):
        config = _make_config(github_enabled=True)
        secrets = _make_secrets(GH_PAT="tok")
        service = DiscoveryService(config=config, secrets=secrets)

        discovery_results = [
            {
                "name": "github",
                "authenticated": True,
                "capabilities": ["read_issue", "create_pr"],
            },
        ]
        catalog = service.build_catalog(discovery_results)
        assert catalog["total_available"] == 1
        assert catalog["total_authenticated"] == 1
        gh = catalog["integrations"][0]
        assert gh["name"] == "github"
        assert gh["authenticated"] is True
        assert gh["capabilities"] == ["read_issue", "create_pr"]

    def test_catalog_includes_unprobed_available_integrations(self):
        config = _make_config(github_enabled=True, slack_enabled=True)
        secrets = _make_secrets(GH_PAT="tok", SLACK_BOT_TOKEN="xoxb")
        service = DiscoveryService(config=config, secrets=secrets)

        discovery_results = [
            {"name": "github", "authenticated": True, "capabilities": ["read"]},
        ]
        catalog = service.build_catalog(discovery_results)
        assert catalog["total_available"] == 2
        names = [i["name"] for i in catalog["integrations"]]
        assert "slack" in names
        slack_entry = next(i for i in catalog["integrations"] if i["name"] == "slack")
        assert slack_entry["authenticated"] is None
        assert "Not probed" in slack_entry["error"]

    def test_catalog_disabled_integration_not_listed(self):
        config = _make_config(github_enabled=True, slack_enabled=False)
        secrets = _make_secrets(GH_PAT="tok", SLACK_BOT_TOKEN="xoxb")
        service = DiscoveryService(config=config, secrets=secrets)

        catalog = service.build_catalog()
        names = [i["name"] for i in catalog["integrations"]]
        assert "slack" not in names

    def test_catalog_no_available_integrations(self):
        config = _make_config(
            github_enabled=False,
            slack_enabled=False,
            jira_enabled=False,
        )
        secrets = _make_secrets()
        service = DiscoveryService(config=config, secrets=secrets)
        catalog = service.build_catalog()
        assert catalog["total_available"] == 0
        assert catalog["total_authenticated"] == 0
        assert catalog["integrations"] == []

    def test_catalog_failed_discovery_shows_unauthenticated(self):
        config = _make_config(github_enabled=True)
        secrets = _make_secrets(GH_PAT="tok")
        service = DiscoveryService(config=config, secrets=secrets)

        discovery_results = [
            {
                "name": "github",
                "authenticated": False,
                "capabilities": [],
                "error": "HTTP 401: Bad credentials",
            },
        ]
        catalog = service.build_catalog(discovery_results)
        assert catalog["total_authenticated"] == 0
        gh = catalog["integrations"][0]
        assert gh["authenticated"] is False
        assert "401" in gh["error"]


# ---------------------------------------------------------------------------
# catalog_as_text
# ---------------------------------------------------------------------------


class TestCatalogAsText:
    def test_text_with_no_integrations(self):
        config = _make_config(
            github_enabled=False,
            slack_enabled=False,
            jira_enabled=False,
        )
        service = DiscoveryService(config=config, secrets=_make_secrets())
        text = service.catalog_as_text()
        assert "Available integrations: 0" in text

    def test_text_with_available_integrations(self):
        config = _make_config(github_enabled=True, slack_enabled=True)
        secrets = _make_secrets(GH_PAT="tok", SLACK_BOT_TOKEN="xoxb")
        service = DiscoveryService(config=config, secrets=secrets)
        text = service.catalog_as_text()
        assert "Available integrations: 2" in text
        assert "- github:" in text
        assert "- slack:" in text
        assert "authenticated=unknown" in text

    def test_text_with_discovery_results(self):
        config = _make_config(github_enabled=True)
        secrets = _make_secrets(GH_PAT="tok")
        service = DiscoveryService(config=config, secrets=secrets)

        discovery_results = [
            {
                "name": "github",
                "authenticated": True,
                "capabilities": ["read_issue", "create_pr"],
            },
        ]
        text = service.catalog_as_text(discovery_results)
        assert "authenticated=yes" in text
        assert "read_issue, create_pr" in text
        assert "(authenticated: 1)" in text

    def test_text_with_error(self):
        config = _make_config(github_enabled=True)
        secrets = _make_secrets(GH_PAT="tok")
        service = DiscoveryService(config=config, secrets=secrets)

        discovery_results = [
            {
                "name": "github",
                "authenticated": False,
                "capabilities": [],
                "error": "HTTP 401",
            },
        ]
        text = service.catalog_as_text(discovery_results)
        assert "authenticated=no" in text
        assert "error: HTTP 401" in text


# ---------------------------------------------------------------------------
# from_config classmethod
# ---------------------------------------------------------------------------


class TestFromConfig:
    def test_github_adapter_created_with_issue_url(self):
        config = EngineConfig()
        config.integrations.github.enabled = True
        secrets = _make_secrets(GH_PAT="test-token")

        service = DiscoveryService.from_config(
            config=config,
            secrets=secrets,
            issue_url="https://github.com/owner/repo/issues/1",
        )
        assert "github" in service.registered_adapters()

    def test_github_adapter_not_created_without_issue_url(self):
        config = EngineConfig()
        config.integrations.github.enabled = True
        secrets = _make_secrets(GH_PAT="test-token")

        service = DiscoveryService.from_config(
            config=config,
            secrets=secrets,
            issue_url="",
        )
        assert "github" not in service.registered_adapters()

    def test_github_adapter_not_created_without_secret(self):
        config = EngineConfig()
        config.integrations.github.enabled = True
        secrets = _make_secrets()

        service = DiscoveryService.from_config(
            config=config,
            secrets=secrets,
            issue_url="https://github.com/owner/repo/issues/1",
        )
        assert "github" not in service.registered_adapters()

    def test_github_disabled_not_created(self):
        config = EngineConfig()
        config.integrations.github.enabled = False
        secrets = _make_secrets(GH_PAT="test-token")

        service = DiscoveryService.from_config(
            config=config,
            secrets=secrets,
            issue_url="https://github.com/owner/repo/issues/1",
        )
        assert "github" not in service.registered_adapters()

    def test_slack_adapter_created(self):
        config = EngineConfig()
        config.integrations.slack.enabled = True
        secrets = _make_secrets(SLACK_BOT_TOKEN="xoxb-test")

        service = DiscoveryService.from_config(config=config, secrets=secrets)
        assert "slack" in service.registered_adapters()

    def test_slack_disabled_not_created(self):
        config = EngineConfig()
        config.integrations.slack.enabled = False
        secrets = _make_secrets(SLACK_BOT_TOKEN="xoxb-test")

        service = DiscoveryService.from_config(config=config, secrets=secrets)
        assert "slack" not in service.registered_adapters()

    def test_slack_no_secret_not_created(self):
        config = EngineConfig()
        config.integrations.slack.enabled = True
        secrets = _make_secrets()

        service = DiscoveryService.from_config(config=config, secrets=secrets)
        assert "slack" not in service.registered_adapters()

    def test_jira_adapter_created(self):
        config = EngineConfig()
        config.integrations.jira.enabled = True
        config.integrations.jira.server_url = "https://jira.example.com"
        secrets = _make_secrets(JIRA_API_TOKEN="jira-test")

        service = DiscoveryService.from_config(config=config, secrets=secrets)
        assert "jira" in service.registered_adapters()

    def test_jira_disabled_not_created(self):
        config = EngineConfig()
        config.integrations.jira.enabled = False
        secrets = _make_secrets(JIRA_API_TOKEN="jira-test")

        service = DiscoveryService.from_config(config=config, secrets=secrets)
        assert "jira" not in service.registered_adapters()

    def test_jira_no_secret_not_created(self):
        config = EngineConfig()
        config.integrations.jira.enabled = True
        secrets = _make_secrets()

        service = DiscoveryService.from_config(config=config, secrets=secrets)
        assert "jira" not in service.registered_adapters()

    def test_all_integrations_created(self):
        config = EngineConfig()
        config.integrations.github.enabled = True
        config.integrations.slack.enabled = True
        config.integrations.slack.channel = "#alerts"
        config.integrations.jira.enabled = True
        config.integrations.jira.server_url = "https://jira.example.com"

        secrets = _make_secrets(
            GH_PAT="gh-tok",
            SLACK_BOT_TOKEN="xoxb-tok",
            JIRA_API_TOKEN="jira-tok",
        )
        service = DiscoveryService.from_config(
            config=config,
            secrets=secrets,
            issue_url="https://github.com/owner/repo/issues/42",
        )
        assert service.registered_adapters() == ["github", "jira", "slack"]

    def test_from_config_with_no_secrets(self):
        config = EngineConfig()
        secrets = _make_secrets()
        service = DiscoveryService.from_config(config=config, secrets=secrets)
        assert service.registered_adapters() == []

    def test_github_adapter_uses_github_token_fallback(self):
        config = EngineConfig()
        config.integrations.github.enabled = True
        secrets = _make_secrets(GITHUB_TOKEN="gha-token")

        service = DiscoveryService.from_config(
            config=config,
            secrets=secrets,
            issue_url="https://github.com/owner/repo/issues/1",
        )
        assert "github" in service.registered_adapters()

    def test_github_non_github_url_skipped(self):
        config = EngineConfig()
        config.integrations.github.enabled = True
        secrets = _make_secrets(GH_PAT="tok")

        service = DiscoveryService.from_config(
            config=config,
            secrets=secrets,
            issue_url="https://gitlab.com/owner/repo/issues/1",
        )
        assert "github" not in service.registered_adapters()

    def test_jira_with_email_for_cloud(self):
        config = EngineConfig()
        config.integrations.jira.enabled = True
        config.integrations.jira.server_url = "https://myteam.atlassian.net"
        secrets = _make_secrets(
            JIRA_API_TOKEN="cloud-tok",
            JIRA_USER_EMAIL="user@example.com",
        )
        service = DiscoveryService.from_config(config=config, secrets=secrets)
        assert "jira" in service.registered_adapters()


# ---------------------------------------------------------------------------
# Integration: from_config → discover_all → build_catalog
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_full_flow_with_fake_adapters(self):
        config = _make_config(github_enabled=True, slack_enabled=True)
        secrets = _make_secrets(GH_PAT="tok", SLACK_BOT_TOKEN="xoxb")
        service = DiscoveryService(config=config, secrets=secrets)

        service.register_adapter(
            FakeAdapter(
                name="github",
                discover_result={
                    "name": "github",
                    "authenticated": True,
                    "capabilities": ["read_issue", "create_pr"],
                },
            )
        )
        service.register_adapter(
            FakeAdapter(
                name="slack",
                discover_result={
                    "name": "slack",
                    "authenticated": True,
                    "capabilities": ["post_message"],
                },
            )
        )

        results = await service.discover_all()
        assert len(results) == 2

        catalog = service.build_catalog(results)
        assert catalog["total_available"] == 2
        assert catalog["total_authenticated"] == 2

        text = service.catalog_as_text(results)
        assert "Available integrations: 2 (authenticated: 2)" in text

    @pytest.mark.asyncio
    async def test_full_flow_with_one_broken_adapter(self):
        config = _make_config(github_enabled=True, slack_enabled=True)
        secrets = _make_secrets(GH_PAT="tok", SLACK_BOT_TOKEN="xoxb")
        service = DiscoveryService(config=config, secrets=secrets)

        service.register_adapter(
            FakeAdapter(
                name="github",
                discover_result={
                    "name": "github",
                    "authenticated": True,
                    "capabilities": ["read_issue"],
                },
            )
        )
        service.register_adapter(
            FakeAdapter(
                name="slack",
                discover_raises=ConnectionError("network down"),
            )
        )

        results = await service.discover_all()
        catalog = service.build_catalog(results)
        assert catalog["total_available"] == 2
        assert catalog["total_authenticated"] == 1

        text = service.catalog_as_text(results)
        assert "authenticated=yes" in text
        assert "authenticated=no" in text
        assert "network down" in text

    @pytest.mark.asyncio
    async def test_discover_all_then_catalog_preserves_capabilities(self):
        service = DiscoveryService()
        service.register_adapter(
            FakeAdapter(
                name="myservice",
                discover_result={
                    "name": "myservice",
                    "authenticated": True,
                    "capabilities": ["alpha", "beta", "gamma"],
                },
            )
        )
        results = await service.discover_all()
        catalog = service.build_catalog(results)
        entry = catalog["integrations"][0]
        assert entry["capabilities"] == ["alpha", "beta", "gamma"]
