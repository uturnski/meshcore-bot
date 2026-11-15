"""Explicitly allowlisted, admin-DM-only remote repeater neighbour snapshots.

!rneighbors                  list configured repeaters (NO RF)
!rneighbors <alias> [page]   view last saved snapshot (NO RF)
!rneighbors refresh <alias> [fetch_page]  request ONE remote page (RF)

Configure [Rneighbors_Command] repeaters = alias=<64hex>,...
No discovery or background polling. Full public keys never appear in replies.
"""
import asyncio
from datetime import datetime, timezone
import json
import math
import re
import sqlite3
import time

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


PUBKEY = re.compile(r"^[0-9a-fA-F]{64}$")
ALIAS = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,19}$")
PAGE_SIZE = 3
MAX_REPLY_BYTES = 158


def _short(text, byte_limit):
    text = " ".join(str(text or "").split())
    if byte_limit < 1:
        return ""
    return text.encode("utf-8")[:byte_limit].decode("utf-8", "ignore")


def _age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _allowlist(raw):
    """An alias-to-exact-key mapping. Reject ambiguity instead of guessing."""
    aliases = {}
    keys = set()
    for entry in re.split(r"[,;\n]+", str(raw or "")):
        entry = entry.strip()
        if not entry:
            continue
        name, delim, key = entry.partition("=")
        name, key = name.strip().lower(), key.strip().lower()
        if not delim or not ALIAS.fullmatch(name) or not PUBKEY.fullmatch(key):
            continue
        if name in aliases or key in keys:
            # Duplicate aliases/keys must not silently overwrite an identity.
            continue
        aliases[name] = key
        keys.add(key)
    return aliases


class RneighborsCommand(BaseCommand):
    name = "rneighbors"
    keywords = ["rneighbors"]
    description = "Query neighbours of explicitly selected repeaters (admin DM)"
    short_description = "Selected remote repeater neighbour snapshots"
    usage = "rneighbors | rneighbors ALIAS [page] | rneighbors refresh ALIAS [fetch_page]"
    examples = ["rneighbors", "rneighbors rep1", "rneighbors refresh rep1", "rneighbors rep1 2"]
    category = "analytics"
    requires_dm = True
    cooldown_seconds = 12

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value(
            "Rneighbors_Command", "enabled", fallback=False, value_type="bool"
        )

    def requires_admin_access(self):
        # Fail closed: the BaseCommand ACL checker only trusts full sender keys.
        return True

    def can_execute(self, message, skip_channel_check=False):
        return (self.enabled and message.is_dm and
                super().can_execute(message, skip_channel_check=skip_channel_check))

    def get_help_text(self):
        return ("Admin DM: rneighbors [ALIAS [page]] (cached), "
                "rneighbors refresh ALIAS [fetch_page] (1 RF request). "
                "Only [Rneighbors_Command] repeaters may be queried.")

    def _selected(self):
        value = self.get_config_value(
            "Rneighbors_Command", "repeaters", fallback="", value_type="str"
        )
        return _allowlist(value)

    def _page_limit(self):
        value = self.get_config_value(
            "Rneighbors_Command", "request_count", fallback=12, value_type="int"
        )
        try:
            return max(1, min(int(value), 24))
        except (TypeError, ValueError):
            return 12

    def _db(self):
        return self.bot.db_manager.connection()

    def _init_schema(self, conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS rneighbors_cache (
            public_key TEXT PRIMARY KEY,
            snapshot_at INTEGER NOT NULL,
            total_count INTEGER NOT NULL,
            results_json TEXT NOT NULL
        )""")

    def _cached(self, key):
        try:
            with self._db() as conn:
                self._init_schema(conn)
                found = conn.execute(
                    "SELECT snapshot_at, total_count, results_json "
                    "FROM rneighbors_cache WHERE public_key=?", (key,)
                ).fetchone()
            if found is None:
                return None
            stamp, count, raw = found
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return {"stamp": int(stamp), "total": int(count), "records": data}
        except (AttributeError, sqlite3.Error, ValueError, TypeError):
            return None

    def _save(self, key, incoming, total, offset):
        """Update only a successfully received requested slice."""
        if not isinstance(incoming, list):
            return False
        stamp = int(time.time())
        try:
            with self._db() as conn:
                self._init_schema(conn)
                existing = conn.execute(
                    "SELECT results_json, total_count FROM rneighbors_cache "
                    "WHERE public_key=?", (key,)
                ).fetchone()
                if offset > 0 and existing:
                    try:
                        records = json.loads(existing[0])
                    except (TypeError, ValueError):
                        records = {}
                    if not isinstance(records, dict):
                        records = {}
                    if int(existing[1]) != int(total):
                        # The repeater's table changed: don't merge inconsistent pages.
                        records = {}
                else:
                    records = {}
                for i, entry in enumerate(incoming):
                    prefix = str(entry.get("pubkey") or "").strip().lower()
                    if not re.fullmatch(r"[a-f0-9]{2,12}", prefix):
                        continue
                    try:
                        secs = max(0, int(entry.get("secs_ago", 0)))
                    except (ValueError, TypeError):
                        secs = 0
                    try:
                        signal = float(entry.get("snr"))
                        signal = signal if math.isfinite(signal) else None
                    except (ValueError, TypeError):
                        signal = None
                    records[str(offset + i)] = {
                        "prefix": prefix, "secs": secs, "snr": signal
                    }
                conn.execute(
                    """INSERT INTO rneighbors_cache
                       (public_key,snapshot_at,total_count,results_json)
                       VALUES(?,?,?,?)
                       ON CONFLICT(public_key) DO UPDATE SET
                       snapshot_at=excluded.snapshot_at,
                       total_count=excluded.total_count,
                       results_json=excluded.results_json""",
                    (key, stamp, max(0, int(total)), json.dumps(records)),
                )
                conn.commit()
            return True
        except (AttributeError, sqlite3.Error, OSError):
            self.logger.exception("Could not save rneighbors snapshot")
            return False

    def _name(self, prefix):
        """Only show a name if the stored key prefix maps to one full key."""
        try:
            with self._db() as conn:
                rows = conn.execute(
                    "SELECT public_key, name FROM complete_contact_tracking "
                    "WHERE lower(public_key) LIKE ? "
                    "ORDER BY last_heard DESC LIMIT 24",
                    (str(prefix).lower() + "%",),
                ).fetchall()
            found = {}
            for key, name in rows:
                found.setdefault(str(key).lower(), name)
            if len(found) == 1:
                return _short(next(iter(found.values())), 13)
        except (AttributeError, sqlite3.Error):
            pass
        return "-"

    async def _reply(self, message, lines):
        budget = min(MAX_REPLY_BYTES, self.get_max_message_length(message))
        result = ""
        for line in lines:
            attempt = result + ("\n" if result else "") + line
            if len(attempt.encode("utf-8")) > budget:
                break
            result = attempt
        return await self.send_response(message, result or "Reply too long")

    async def _refresh(self, message, alias, key, fetch_page):
        count = self._page_limit()
        offset = (fetch_page - 1) * count
        if offset > 255 * 255:
            return await self._reply(message, ["Fetch page too large"])
        mesh = getattr(self.bot, "meshcore", None)
        commands = getattr(mesh, "commands", None)
        getter = getattr(commands, "req_neighbours_sync", None)
        if not callable(getter):
            return await self._reply(message, [
                "Remote neighbour API unavailable.",
                "Update the MeshCore Python library."
            ])
        # One *explicitly requested* remote request. Never fetch_all_neighbours().
        try:
            result = await asyncio.wait_for(
                getter(key, count=count, offset=offset,
                       order_by=0, pubkey_prefix_length=6, timeout=12),
                timeout=18,
            )
        except asyncio.TimeoutError:
            result = None
        except Exception as exc:
            self.logger.warning("rneighbors %s query failed: %s", alias, exc)
            result = None
        if not isinstance(result, dict) or not isinstance(result.get("neighbours"), list):
            return await self._reply(message, [
                f"{alias}: no remote neighbour response.",
                "Check repeater access/range/firmware."
            ])
        neighbours = result["neighbours"]
        try:
            total = max(0, int(result.get("neighbours_count", len(neighbours))))
        except (ValueError, TypeError):
            total = len(neighbours)
        if not self._save(key, neighbours, total, offset):
            return await self._reply(message, [
                f"{alias}: reply received but cache failed.",
                "No results saved."
            ])
        return await self._reply(message, [
            f"{alias}: saved {len(neighbours)}/{total}",
            f"RF fetch page {fetch_page}.",
            f"View: !rneighbors {alias}",
            ("More: !rneighbors refresh " + alias + " " + str(fetch_page + 1)
             if offset + len(neighbours) < total else "Snapshot updated")
        ])

    async def execute(self, message: MeshMessage):
        # Explicit check also protects direct execute() calls.
        if not message.is_dm or not self._check_admin_access(message):
            return False
        parts = str(message.content or "").strip().removeprefix("!").split()
        aliases = self._selected()
        if not parts or parts[0].lower() != "rneighbors":
            return await self._reply(message, [self.get_help_text()])
        if len(parts) == 1:
            if not aliases:
                return await self._reply(message, [
                    "No selected repeaters.",
                    "Set [Rneighbors_Command] repeaters."
                ])
            lines = [f"Selected repeaters ({len(aliases)}):"]
            for i, (name, key) in enumerate(aliases.items(), 1):
                lines.append(f"{i}. {name} {key[:12]}")
            lines.append("!rneighbors ALIAS / refresh ALIAS")
            return await self._reply(message, lines)

        refresh = len(parts) >= 2 and parts[1].lower() == "refresh"
        if refresh:
            if len(parts) not in (3, 4):
                return await self._reply(message, [self.get_help_text()])
            alias = parts[2].lower()
            page_arg = parts[3] if len(parts) == 4 else "1"
        else:
            if len(parts) not in (2, 3):
                return await self._reply(message, [self.get_help_text()])
            alias = parts[1].lower()
            page_arg = parts[2] if len(parts) == 3 else "1"
        if alias not in aliases:
            return await self._reply(message, [
                "Repeater not selected.",
                "Use !rneighbors to list allowed aliases."
            ])
        if not re.fullmatch(r"[1-9][0-9]{0,3}", page_arg):
            return await self._reply(message, ["Page must be 1-9999"])
        page = int(page_arg)
        key = aliases[alias]
        if refresh:
            return await self._refresh(message, alias, key, page)

        snapshot = self._cached(key)
        if snapshot is None:
            return await self._reply(message, [
                f"{alias}: no cached neighbours.",
                f"Run !rneighbors refresh {alias}"
            ])
        positions = sorted(int(p) for p in snapshot["records"] if p.isdigit())
        total_pages = max(1, math.ceil(len(positions) / PAGE_SIZE))
        if page > total_pages:
            return await self._reply(message, [f"Only {total_pages} cached pages for {alias}."])
        start = (page - 1) * PAGE_SIZE
        positions = positions[start:start + PAGE_SIZE]
        stamp_age = max(0, int(time.time()) - snapshot["stamp"])
        lines = [
            f"{alias}: {len(snapshot['records'])}/{snapshot['total']} neighbours",
            f"Cached {_age(stamp_age)} | {page}/{total_pages}"
        ]
        for index in positions:
            record = snapshot["records"][str(index)]
            prefix = record["prefix"]
            signal = record.get("snr")
            snr = f"{signal:+g}" if isinstance(signal, (int, float)) else "?"
            heard = _age(stamp_age + max(0, int(record.get("secs", 0))))
            lines.append(
                f"{index + 1}. {_short(self._name(prefix), 11)} "
                f"{prefix[:8]} S{snr} {heard}"
            )
        return await self._reply(message, lines)
