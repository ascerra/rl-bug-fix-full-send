"""Slack integration adapter — post notifications, read channel history.

Implements the IntegrationAdapter protocol (SPEC §9.2) with methods for
posting messages, reading channel history (with injection guards), and
listing channels. Uses httpx for async HTTP against the Slack Web API.
Token from SLACK_BOT_TOKEN env var.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from engine.config import SlackIntegrationConfig

API_BASE = "https://slack.com/api"
DEFAULT_TIMEOUT_S = 30
MAX_HISTORY_MESSAGES = 100
DEFAULT_HISTORY_LIMIT = 20

UNTRUSTED_CONTENT_DELIMITER = "--- UNTRUSTED SLACK CONTENT BELOW ---"


class SlackAdapterError(Exception):
    """Raised for unrecoverable Slack API errors."""


class SlackAdapter:
    """Slack Web API adapter implementing IntegrationAdapter.

    Provides high-level typed methods (``post_message``, ``read_history``,
    etc.) and the generic ``discover``/``read``/``write``/``search`` protocol.

    Resource IDs use ``type/identifier`` format:
    - ``channel/{id}/messages`` — message history for a channel
    - ``channel/{id}/post`` — post a message to a channel

    All content read from Slack is treated as untrusted input per SPEC §7
    principle 3 (prompts never mix trusted and untrusted content).
    """

    name = "slack"

    def __init__(
        self,
        token: str | None = None,
        config: SlackIntegrationConfig | None = None,
    ):
        self._token = token or os.environ.get("SLACK_BOT_TOKEN", "")
        self.config = config or SlackIntegrationConfig()

    @property
    def default_channel(self) -> str:
        return self.config.channel

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # ------------------------------------------------------------------
    # Generic IntegrationAdapter protocol
    # ------------------------------------------------------------------

    async def discover(self) -> dict[str, Any]:
        """Check authentication and list capabilities."""
        capabilities = [
            "post_message",
            "read_history",
            "list_channels",
        ]

        result: dict[str, Any] = {
            "name": self.name,
            "authenticated": False,
            "capabilities": capabilities,
            "default_channel": self.default_channel,
        }

        if not self._token:
            result["error"] = "No Slack token available (SLACK_BOT_TOKEN)"
            return result

        resp = await self._api_call("auth.test")
        if resp.get("ok"):
            result["authenticated"] = True
            result["team"] = resp.get("team", "")
            result["user"] = resp.get("user", "")
            result["team_id"] = resp.get("team_id", "")
            result["bot_id"] = resp.get("bot_id", "")
        else:
            result["error"] = resp.get("error", "Authentication failed")

        return result

    async def read(self, resource_id: str) -> dict[str, Any]:
        """Read a resource by type/identifier.

        Supported formats:
        - ``channel/{id}/messages`` — read channel message history
        """
        parts = resource_id.strip("/").split("/")
        if len(parts) < 2:
            return {"success": False, "error": f"Invalid resource_id: {resource_id}"}

        rtype = parts[0]

        if rtype == "channel" and len(parts) == 3 and parts[2] == "messages":
            return await self.read_history(parts[1])

        return {"success": False, "error": f"Unknown resource format: {resource_id}"}

    async def write(self, resource_id: str, content: dict[str, Any]) -> dict[str, Any]:
        """Write to a resource by type/identifier.

        Supported formats:
        - ``channel/{id}/post`` — post a message (content: text)
        - ``notification`` — post to default channel (content: text, level)
        """
        parts = resource_id.strip("/").split("/")
        if not parts:
            return {"success": False, "error": f"Invalid resource_id: {resource_id}"}

        rtype = parts[0]

        if rtype == "channel" and len(parts) == 3 and parts[2] == "post":
            return await self.post_message(
                channel=parts[1],
                text=content.get("text", ""),
            )

        if rtype == "notification" and len(parts) == 1:
            return await self.post_notification(
                text=content.get("text", ""),
                level=content.get("level", "info"),
            )

        return {"success": False, "error": f"Unknown write target: {resource_id}"}

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Search channels matching the query name."""
        result = await self.list_channels()
        if not result.get("success"):
            return []

        query_lower = query.lower()
        return [
            ch
            for ch in result.get("channels", [])
            if query_lower in ch.get("name", "").lower()
            or query_lower in ch.get("purpose", "").lower()
        ]

    # ------------------------------------------------------------------
    # High-level typed methods
    # ------------------------------------------------------------------

    async def post_message(self, channel: str, text: str) -> dict[str, Any]:
        """Post a message to a Slack channel."""
        if not text:
            return {"success": False, "error": "Message text is required"}
        if not channel:
            return {"success": False, "error": "Channel is required"}

        resp = await self._api_call(
            "chat.postMessage",
            json_body={"channel": channel, "text": text},
        )
        if not resp.get("ok"):
            return {"success": False, "error": resp.get("error", "Post failed")}

        return {
            "success": True,
            "channel": resp.get("channel", channel),
            "ts": resp.get("ts", ""),
            "message": resp.get("message", {}),
        }

    async def post_notification(
        self,
        text: str,
        level: str = "info",
    ) -> dict[str, Any]:
        """Post a notification to the configured default channel.

        Convenience method for loop completion notifications. The ``level``
        parameter is prepended as an emoji prefix.
        """
        channel = self.default_channel
        if not channel:
            return {"success": False, "error": "No default channel configured"}

        prefix_map = {
            "success": ":white_check_mark:",
            "failure": ":x:",
            "escalation": ":warning:",
            "info": ":information_source:",
        }
        prefix = prefix_map.get(level, prefix_map["info"])
        formatted_text = f"{prefix} {text}"

        return await self.post_message(channel=channel, text=formatted_text)

    async def read_history(
        self,
        channel: str,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> dict[str, Any]:
        """Read recent message history from a channel.

        All message content is wrapped with injection guard delimiters,
        treating it as untrusted input per SPEC §7 principle 3.
        """
        if not channel:
            return {"success": False, "error": "Channel is required"}

        capped_limit = min(limit, MAX_HISTORY_MESSAGES)
        resp = await self._api_call(
            "conversations.history",
            json_body={"channel": channel, "limit": capped_limit},
        )
        if not resp.get("ok"):
            return {"success": False, "error": resp.get("error", "History read failed")}

        raw_messages = resp.get("messages", [])
        if not isinstance(raw_messages, list):
            raw_messages = []

        messages = [
            {
                "user": msg.get("user", ""),
                "text": _wrap_untrusted(msg.get("text", "")),
                "ts": msg.get("ts", ""),
                "type": msg.get("type", "message"),
            }
            for msg in raw_messages
        ]
        return {
            "success": True,
            "channel": channel,
            "messages": messages,
            "has_more": resp.get("has_more", False),
        }

    async def list_channels(
        self,
        limit: int = MAX_HISTORY_MESSAGES,
    ) -> dict[str, Any]:
        """List public channels the bot can see."""
        resp = await self._api_call(
            "conversations.list",
            json_body={
                "limit": min(limit, MAX_HISTORY_MESSAGES),
                "types": "public_channel",
                "exclude_archived": True,
            },
        )
        if not resp.get("ok"):
            return {"success": False, "error": resp.get("error", "Channel list failed")}

        raw_channels = resp.get("channels", [])
        if not isinstance(raw_channels, list):
            raw_channels = []

        channels = [
            {
                "id": ch.get("id", ""),
                "name": ch.get("name", ""),
                "purpose": ch.get("purpose", {}).get("value", "")
                if isinstance(ch.get("purpose"), dict)
                else "",
                "num_members": ch.get("num_members", 0),
                "is_member": ch.get("is_member", False),
            }
            for ch in raw_channels
        ]
        return {"success": True, "channels": channels}

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    async def _api_call(
        self,
        method: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated call to the Slack Web API.

        Slack API always returns HTTP 200 with ``ok: true/false`` in the body.
        We return the parsed body directly.
        """
        url = f"{API_BASE}/{method}"
        headers = self._headers()

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json=json_body or {},
                )
                try:
                    return response.json()
                except ValueError:
                    return {"ok": False, "error": f"Invalid JSON response: {response.text[:500]}"}
            except httpx.HTTPError as exc:
                return {"ok": False, "error": f"HTTP error: {exc}"}


def _wrap_untrusted(text: str) -> str:
    """Wrap Slack message content with untrusted content delimiters.

    Slack messages are user-generated and must never be trusted as instructions.
    This wrapping makes it clear to LLM consumers that the content is untrusted.
    """
    if not text:
        return text
    return f"{UNTRUSTED_CONTENT_DELIMITER}\n{text}\n--- END UNTRUSTED SLACK CONTENT ---"
