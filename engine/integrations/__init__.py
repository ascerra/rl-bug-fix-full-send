"""Integration adapters for external systems.

Each integration implements the IntegrationAdapter protocol, providing a
uniform interface for discovery, reading, writing, and searching resources
across GitHub, Slack, Jira, and other services.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IntegrationAdapter(Protocol):
    """Protocol for external system integration adapters (SPEC §9.2).

    Every integration implements discover/read/write/search. Resource IDs
    use a ``type/identifier`` format (e.g., ``issue/123``, ``pr/456``).
    """

    name: str

    async def discover(self) -> dict[str, Any]:
        """Return capabilities and available resources."""
        ...

    async def read(self, resource_id: str) -> dict[str, Any]:
        """Read a resource (issue, PR, comment, CI status)."""
        ...

    async def write(self, resource_id: str, content: dict[str, Any]) -> dict[str, Any]:
        """Write to a resource (comment, label, PR)."""
        ...

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Search for resources matching a query."""
        ...
