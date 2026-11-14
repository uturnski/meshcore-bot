"""Readable route-aware ping reply for MeshCore Bot.

Installed as a standard *alternative* for the built-in ping command:

[Plugin_Overrides]
ping = ping_diagnostics

The upstream local plugin loader skips duplicate primary names; therefore a
replacement for ping must live in modules/commands/alternatives. No RF discovery,
network lookups, or probing are performed. Repeater names come from stored data.
"""
import math
import re

from ...models import MeshMessage
from ..base_command import BaseCommand


def _short_utf8(text: str, maximum: int) -> str:
    """Limit string by UTF-8 bytes, never cutting a multibyte codepoint."""
    raw = str(text)
    if len(raw.encode("utf-8")) <= maximum:
        return raw
    if maximum <= 3:
        return "." * maximum
    result = ""
    for char in raw:
        if len((result + char).encode("utf-8")) > maximum - 3:
            break
        result += char
    return result.rstrip() + "..."


def _route_nodes(message: MeshMessage) -> list[str]:
    """Use only this message's correlated RF path, not an unrelated recent packet."""
    info = getattr(message, "routing_info", None) or {}
    raw = info.get("path_nodes")
    if isinstance(raw, (tuple, list)) and raw:
        nodes = [str(node).strip().lower() for node in raw]
        if all(re.fullmatch(r"[0-9a-f]{2}(?:[0-9a-f]{2}){0,2}", node) for node in nodes):
            return nodes

    # Some firmware exposes path_hex and an explicit bytes-per-hop count instead.
    path_hex = str(info.get("path_hex") or "").strip().lower()
    try:
        bph = int(info.get("bytes_per_hop") or 0)
        count = int(info.get("path_length") or 0)
    except (TypeError, ValueError):
        bph, count = 0, 0
    if 1 <= bph <= 3 and 1 <= count <= 64 and len(path_hex) == 2 * bph * count:
        if re.fullmatch(r"[0-9a-f]+", path_hex):
            return [path_hex[i:i + bph * 2] for i in range(0, len(path_hex), bph * 2)]

    # The upstream message handler puts "aa,bb (2 hops via ...)" in message.path.
    path_text = str(getattr(message, "path", "") or "").split(" (", 1)[0].strip().lower()
    parts = [part.strip() for part in path_text.split(",")]
    if parts and all(re.fullmatch(r"[0-9a-f]{2}(?:[0-9a-f]{2}){0,2}", part) for part in parts):
        return parts
    return []


def _names_from_records(nodes: list[str], records: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in nodes:
        matches: dict[str, str] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            key = str(record.get("public_key") or "").strip().lower()
            if not key.startswith(node):
                continue
            role = str(record.get("role") or record.get("device_type") or "").lower()
            if role not in ("repeater", "roomserver"):
                continue
            if key not in matches:
                matches[key] = str(record.get("name") or "").strip()
        # Ambiguous public-key prefixes are deliberately not guessed.
        names[node] = next(iter(matches.values())) if len(matches) == 1 else "n/a"
        if not names[node]:
            names[node] = "n/a"
    return names


class PingDiagnosticsCommand(BaseCommand):
    name = "ping"
    keywords = ["ping", "pingplain"]
    description = "Reply with SNR, hop count, and recorded repeater names along the route"
    short_description = "Route-aware ping with repeater names"
    usage = "ping (or pingplain for original reply)"
    examples = ["ping", "pingplain"]
    category = "meshcore_info"
    cooldown_seconds = 2

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value(
            "Ping_Command", "enabled", fallback=True, value_type="bool"
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        return self.enabled and super().can_execute(message, skip_channel_check)

    def get_response_format(self):
        # If the inherited PingCommand returned [Keywords] ping, the command manager
        # would pre-render a static reply, bypassing dynamic path-name resolution.
        return None

    def get_help_text(self):
        return (
            "ping: DM replies with sender, hops, SNR and known repeater names "
            "for this received path. pingplain: original short ping."
        )

    async def _lookup_names(self, nodes: list[str]) -> dict[str, str]:
        manager = getattr(self.bot, "repeater_manager", None)
        records = []
        if manager is not None:
            try:
                records = await manager.get_repeater_devices(include_historical=True)
            except Exception as exc:
                self.logger.debug("Ping: repeater manager lookup failed: %s", exc)
        names = _names_from_records(nodes, records or [])
        unresolved = [node for node in nodes if names.get(node, "n/a") == "n/a"]
        if not unresolved:
            return names

        db_manager = getattr(self.bot, "db_manager", None)
        if db_manager is None:
            return names
        try:
            with db_manager.connection() as conn:
                for node in unresolved:
                    rows = conn.execute(
                        """SELECT public_key, name, role
                             FROM complete_contact_tracking
                            WHERE lower(public_key) LIKE ?
                              AND lower(role) IN ('repeater', 'roomserver')""",
                        (node + "%",),
                    ).fetchall()
                    db_matches = _names_from_records(
                        [node],
                        [{"public_key": row[0], "name": row[1], "role": row[2]} for row in rows],
                    )
                    if db_matches[node] != "n/a":
                        names[node] = db_matches[node]
        except Exception as exc:
            self.logger.debug("Ping: stored name lookup unavailable: %s", exc)
        return names

    def _legacy_reply(self, message: MeshMessage) -> str:
        value = None
        if self.bot.config.has_section("Keywords"):
            value = self.bot.config.get("Keywords", "ping", fallback=None)
        template = self._strip_quotes_from_config(value) if value else "Pong! | {connection_info}"
        return self.format_response(message, template)

    async def execute(self, message: MeshMessage) -> bool:
        # Preserve an explicit escape hatch for the upstream configurable ping.
        first = str(message.content or "").strip().lstrip("!").split(maxsplit=1)
        if first and first[0].lower() == "pingplain":
            return await self.send_response(message, self._legacy_reply(message))

        sender = _short_utf8(str(message.sender_id or "Unknown"), 32)
        try:
            snr = float(message.snr)
            snr_text = f"{snr:g}" if math.isfinite(snr) else "n/a"
        except (ValueError, TypeError):
            snr_text = "n/a"

        nodes = _route_nodes(message)
        try:
            reported_hops = int(message.hops) if message.hops is not None else None
        except (ValueError, TypeError):
            reported_hops = None
        hops = reported_hops if reported_hops is not None and 0 <= reported_hops < 255 else len(nodes)
        label = "hop" if hops == 1 else "hops"
        header = f"@[{sender}] {hops} {label}, SNR {snr_text}"
        budget = self.get_max_message_length(message)
        if len(header.encode("utf-8")) > budget:
            header = _short_utf8(header, budget)

        # Channel pings stay brief; route lists are sent only by DM.
        if not message.is_dm or not nodes:
            return await self.send_response(message, header)

        name_map = await self._lookup_names(nodes)
        lines = [
            f"{node}: {_short_utf8(name_map.get(node, 'n/a'), 32)}"
            for node in nodes
        ]
        response = header + "\n" + "\n".join(lines)
        if len(response.encode("utf-8")) > budget:
            # Shrink the longest names first to keep every hop visible when possible.
            labels = [name_map.get(node, "n/a") for node in nodes]
            while True:
                response = header + "\n" + "\n".join(
                    f"{node}: {label}" for node, label in zip(nodes, labels)
                )
                if len(response.encode("utf-8")) <= budget:
                    break
                idx = max(range(len(labels)), key=lambda i: len(labels[i].encode("utf-8")))
                if len(labels[idx].encode("utf-8")) <= 3:
                    break
                # Reduce the largest name, retaining UTF-8 and a visible abbreviation.
                shorter = _short_utf8(labels[idx].removesuffix("..."), len(labels[idx].encode("utf-8")) - 1)
                if shorter == labels[idx]:
                    break
                labels[idx] = shorter

        if len(response.encode("utf-8")) > budget:
            # Too many hops to fit: use full lines until the payload limit.
            response = header
            for i, line in enumerate(lines):
                candidate = response + "\n" + line
                if len(candidate.encode("utf-8")) > budget:
                    tail = f"\n+{len(lines) - i} more hops"
                    if len((response + tail).encode("utf-8")) <= budget:
                        response += tail
                    break
                response = candidate
        return await self.send_response(message, response)
