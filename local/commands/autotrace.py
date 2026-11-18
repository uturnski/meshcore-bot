"""Suggest *user-originated* MeshCore trace routes from passive DM observations.

NEVER sends a trace. The requesting user executes a suggested route on their own
companion. All observed routes flow sender -> repeaters -> this bot; joining
two routes at a shared repeater implies reverse-direction links and MUST be
reported as an unverified candidate, not proof of bidirectional RF connectivity.
"""
from datetime import datetime, timedelta
import hashlib
import heapq
import re
import sqlite3
import time

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

PUBKEY = re.compile(r"^[0-9a-fA-F]{64}$")
NODE = re.compile(r"^[0-9a-fA-F]{2}(?:[0-9a-fA-F]{2})?$")
PATH = re.compile(
    r"^([0-9a-fA-F]{2,6}(?:\s*,\s*[0-9a-fA-F]{2,6})*)"
    r"(?:\s*\(|\s*$)"
)


def parse_route(text, max_hops=5):
    """Parse a complete observed path. Reject mixed widths, >2-byte or truncation."""
    m = PATH.match(str(text or "").strip())
    if not m:
        return None
    nodes = tuple(x.strip().lower() for x in m.group(1).split(","))
    if (not nodes or len(nodes) > max_hops
            or len({len(x) for x in nodes}) != 1
            or not all(NODE.fullmatch(x) for x in nodes)):
        return None
    return nodes


def candidate_route(source, target, max_hops=5):
    """Join two sender->bot histories at their first common repeater toward bot.

    Every outbound direction after the junction is a reversed *observation*,
    not a validated bidirectional link. Require same on-air hash width.
    """
    if not source or not target or len(source[0]) != len(target[0]):
        return None
    shared = 0
    for left, right in zip(reversed(source), reversed(target)):
        if left != right:
            break
        shared += 1
    if not shared:
        return None
    path = source[:len(source) - shared + 1] + tuple(
        reversed(target[:len(target) - shared])
    )
    if len(path) > max_hops or len(set(path)) != len(path):
        return None
    return path


def wait_label(seconds):
    seconds = max(1, int(seconds + .999))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class AutotraceCommand(BaseCommand):
    name = "autotrace"
    keywords = ["autotrace"]
    description = "Suggest user-to-target trace routes using observed advertisement paths"
    short_description = "User-originated route suggestions; no bot trace probes"
    usage = "autotrace [paths|NODE [paths|1|2|3]]"
    examples = ["autotrace Dee", "autotrace a1b2c3", "autotrace Dee paths"]
    category = "meshcore_info"
    requires_dm = True
    # Only the command reply uses RF. The trace itself must be run by the user.
    cooldown_seconds = 0
    enabled_attr = "enabled"
    settings_schema = [
        {"key": "enabled", "label": "Enable AutoTrace", "type": "bool", "default": False},
        {"key": "per_user_cooldown_seconds", "label": "User reply cooldown",
         "type": "int", "min": 0, "default": 900},
        {"key": "global_cooldown_seconds", "label": "Global reply cooldown",
         "type": "int", "min": 0, "default": 60},
        {"key": "maximum_hops", "label": "Maximum candidate hops",
         "type": "int", "min": 1, "max": 5, "default": 5},
        {"key": "max_route_age_hours", "label": "Observation age",
         "type": "int", "min": 1, "max": 168, "default": 24},
        {"key": "graph_edge_age_hours", "label": "Maximum graph-edge age",
         "type": "int", "min": 1, "max": 168, "default": 72},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value(
            "Autotrace_Command", "enabled", fallback=False, value_type="bool"
        )
        self.per_user_seconds = max(0, self._int("per_user_cooldown_seconds", 900))
        self.global_seconds = max(0, self._int("global_cooldown_seconds", 60))
        self.max_hops = max(1, min(5, self._int("maximum_hops", 5)))
        self.route_age_hours = max(1, min(168, self._int("max_route_age_hours", 24)))
        self.graph_edge_hours = max(1, min(168, self._int("graph_edge_age_hours", 72)))
        self._last_user = {}
        self._last_global = float("-inf")

    def _int(self, key, default):
        try:
            return int(self.get_config_value(
                "Autotrace_Command", key, fallback=default, value_type="int"
            ))
        except (TypeError, ValueError):
            return default

    def can_execute(self, message, skip_channel_check=False):
        return self.enabled and message.is_dm and super().can_execute(
            message, skip_channel_check=skip_channel_check
        )

    def get_help_text(self):
        return ("DM: autotrace NODE [paths|1|2|3]. "
                "Candidate route FROM your radio; execute trace yourself. No bot probe.")

    def _label(self, value):
        label = str(value or "").strip()
        if not label or label.casefold() == "unknown":
            return None
        if self.bot.config.getboolean("Stats_Command", "anonymize_users", fallback=False):
            return "user_" + hashlib.md5(label.encode("utf-8")).hexdigest()[:8]
        return label

    def _identity(self, message):
        key = str(getattr(message, "sender_pubkey", "") or "").lower().strip()
        return key if PUBKEY.fullmatch(key) else None

    def _contacts(self, conn):
        return [
            (str(name or ""), str(key or "").strip().lower(), str(role or "").lower())
            for name, key, role in conn.execute(
                "SELECT name, public_key, role FROM complete_contact_tracking"
            ).fetchall()
            if PUBKEY.fullmatch(str(key or "").strip())
        ]

    def _resolve_target(self, query, contacts):
        term = str(query or "").strip()
        if re.fullmatch(r"[0-9a-fA-F]{6}", term):
            matches = [c for c in contacts if c[1].startswith(term.lower())]
        else:
            matches = [c for c in contacts if c[0].casefold() == term.casefold()]
            if not matches:
                matches = [c for c in contacts if term.casefold() in c[0].casefold()]
        identities = {c[1]: c for c in matches}
        if not identities:
            return None, "AutoTrace: target not found"
        if len(identities) != 1:
            return None, "AutoTrace: ambiguous; use 3-byte ID"
        return next(iter(identities.values())), None

    @staticmethod
    def _decode_observed(path_hex, bytes_per_hop):
        """Decode original on-air hash width; do not split 3-byte hops as 1-byte."""
        try:
            width = int(bytes_per_hop)
        except (ValueError, TypeError):
            return None
        raw = str(path_hex or "").strip().lower()
        chars = 2 * width
        if width not in (1, 2, 3) or not raw or len(raw) % chars:
            return None
        if not re.fullmatch(r"[a-f0-9]+", raw):
            return None
        nodes = tuple(raw[i:i + chars] for i in range(0, len(raw), chars))
        # At most 16 observed hops are decoded; only a final candidate is capped
        return nodes if 0 < len(nodes) <= 16 else None

    @classmethod
    def _incoming_dm_route(cls, message):
        """Current sender->bot route; never a route from a different node.

        Returns (nodes, status). Direct, missing and malformed are distinct
        so a zero-hop packet cannot be mistaken for an absent record.
        """
        info = getattr(message, "routing_info", None) or {}
        if not isinstance(info, dict):
            info = {}
        raw_nodes = info.get("path_nodes")
        try:
            hop_count = int(info["path_length"]) if info.get("path_length") is not None else None
        except (TypeError, ValueError):
            hop_count = None
        if isinstance(raw_nodes, (list, tuple)) and raw_nodes:
            nodes = tuple(str(node).strip().lower() for node in raw_nodes)
            if (len(nodes) <= 16 and len({len(x) for x in nodes}) == 1
                    and len(nodes[0]) in (2, 4, 6)
                    and all(re.fullmatch(r"[0-9a-f]+", x) for x in nodes)
                    and (hop_count is None or hop_count == len(nodes))):
                return nodes, "routed"

        path_hex = str(info.get("path_hex") or "").strip()
        bph = info.get("bytes_per_hop")
        if path_hex and bph is not None:
            nodes = cls._decode_observed(path_hex, bph)
            if nodes and (hop_count is None or hop_count == len(nodes)):
                return nodes, "routed"

        plain = str(getattr(message, "path", "") or "").strip()
        # The upstream handler displays missing path_len=255 as Direct (0 hops).
        # Only a correlated RF record with explicit zero hops proves direct.
        if hop_count == 0:
            return None, "direct"
        # Keep the whole observed sequence (maximum 16), not a truncated path.
        m = PATH.match(plain)
        if m:
            parts = tuple(piece.strip().lower() for piece in m.group(1).split(","))
            if (len(parts) <= 16 and len({len(x) for x in parts}) == 1
                    and len(parts[0]) in (2, 4, 6)
                    and all(re.fullmatch(r"[a-f0-9]+", x) for x in parts)
                    and (hop_count is None or hop_count == len(parts))):
                return parts, "routed"
        return None, "missing"

    def _observed(self, conn, public_key):
        """Advertisement paths are bound to their full originating public key."""
        # observed_paths.last_seen is written with naive local datetime.now().
        cutoff = (datetime.now() - timedelta(
            hours=self.route_age_hours
        )).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            """SELECT path_hex, bytes_per_hop, observation_count, last_seen
               FROM observed_paths
               WHERE packet_type='advert' AND lower(public_key)=?
                 AND datetime(last_seen)>=datetime(?)
               ORDER BY last_seen DESC, observation_count DESC LIMIT 250""",
            (public_key.lower(), cutoff),
        ).fetchall()
        result, seen = [], set()
        for raw, width, count, stamp in rows:
            nodes = self._decode_observed(raw, width)
            if nodes and nodes not in seen:
                result.append((nodes, stamp, int(count or 1)))
                seen.add(nodes)
        return result

    @staticmethod
    def _repeater_keys(contacts):
        return {c[1] for c in contacts if c[2] in ("repeater", "roomserver")}

    @staticmethod
    def _keyed_nodes(nodes, repeater_keys):
        """Resolve EVERY on-air hash to exactly one known repeater full key.

        Mixed original path widths can then be joined without incorrectly
        equating two different repeaters sharing a short hash.
        """
        result = []
        for hop in nodes:
            matches = [key for key in repeater_keys if key.startswith(hop)]
            if len(matches) != 1:
                return None
            result.append(matches[0])
        return tuple(result)

    def _manual_route(self, source, target, all_keys, is_repeater):
        """Join source->bot and target->bot paths; output a user-originated route.

        Target companion is not a trace hop: final node is its first observed
        repeater. For repeater targets, append that repeater itself.
        """
        # Paths share a common suffix (toward the bot), not an arbitrary
        # co-location claim or a connection inferred from geography.
        if not source or not target:
            return None
        shared = 0
        for a, b in zip(reversed(source), reversed(target)):
            if a != b:
                break
            shared += 1
        if shared < 1:
            return None
        joined = source[:len(source) - shared + 1] + tuple(
            reversed(target[:len(target) - shared])
        )
        # For a repeater's own advert the repeater is the origin, rather
        # than a hop listed in path_hex.
        if is_repeater:
            joined += (is_repeater,)
        if not joined or len(joined) > self.max_hops or len(set(joined)) != len(joined):
            return None
        # The local trace engine currently only supports 1-/2-byte path mode.
        # Encode with 2 bytes if needed; refuse ambiguous or unavailable hashes.
        for width in (1, 2):
            count = width * 2
            if len({node[:count] for node in joined}) != len(joined):
                continue
            if any(sum(k.startswith(node[:count]) for k in all_keys) != 1 for node in joined):
                continue
            return tuple(node[:count] for node in joined)
        return None

    @staticmethod
    def _unique_graph_identity(prefix, recorded_key, candidates):
        """Resolve an endpoint to exactly one full public key."""
        short = str(prefix or "").strip().lower()
        stored = str(recorded_key or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{2,64}", short) or len(short) % 2:
            return None
        if stored and (not PUBKEY.fullmatch(stored) or not stored.startswith(short)
                       or stored not in candidates):
            return None
        matches = {key for key in candidates if key.startswith(short)}
        if len(matches) != 1:
            return None
        key = next(iter(matches))
        return key if not stored or stored == key else None

    @staticmethod
    def _encode_graph_route(full_keys, known_repeaters):
        """Emit only traceable, uniquely identifiable one-/two-byte hashes."""
        for width in (2, 4):
            tokens = tuple(k[:width] for k in full_keys)
            if len(set(tokens)) == len(tokens) and all(
                sum(k.startswith(token) for k in known_repeaters) == 1
                for token in tokens
            ):
                return tokens
        return None

    def _graph_candidates(self, conn, source_key, target_key, target_name,
                          target_role, target_adverts, contacts):
        """Build bounded hypotheses from source->relay and repeater graph rows.

        A recorded source->first-relay edge is mandatory; remaining graph
        connections are directional observations treated as unverified
        reverse-capable links, never as proven RF connectivity.
        """
        repeaters = self._repeater_keys(contacts)
        all_contacts = {c[1] for c in contacts} | {source_key}
        cutoff = (datetime.now() - timedelta(
            hours=self.graph_edge_hours
        )).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            """SELECT from_prefix, to_prefix, from_public_key, to_public_key,
                      observation_count, last_seen
               FROM mesh_connections
               WHERE datetime(last_seen)>=datetime(?)
               ORDER BY observation_count DESC, last_seen DESC LIMIT 8000""",
            (cutoff,),
        ).fetchall()
        first_hops = {}
        adjacency = {}
        for from_pre, to_pre, from_key, to_key, count, stamp in rows:
            left_pre = str(from_pre or "").strip().lower()
            right_pre = str(to_pre or "").strip().lower()
            n = max(1, int(count or 1))
            if (len(left_pre) >= 6 and source_key.startswith(left_pre)
                    and self._unique_graph_identity(
                        left_pre, from_key, all_contacts
                    ) == source_key):
                neighbor = self._unique_graph_identity(
                    right_pre, to_key, repeaters
                )
                if neighbor:
                    first_hops[neighbor] = max(first_hops.get(neighbor, 0), n)
            left = self._unique_graph_identity(left_pre, from_key, repeaters)
            right = self._unique_graph_identity(right_pre, to_key, repeaters)
            if left and right and left != right:
                adjacency.setdefault(left, {})[right] = max(
                    n, adjacency.get(left, {}).get(right, 0)
                )
                adjacency.setdefault(right, {})[left] = max(
                    n, adjacency.get(right, {}).get(left, 0)
                )
        if not first_hops:
            return [], "AutoTrace: no recent user-to-repeater link"
        # A companion target's first advertised repeater is the terminal
        # diagnostic hop. A repeater target itself is the terminal hop.
        terminals = set()
        if target_role in ("repeater", "roomserver"):
            terminals.add(target_key)
        else:
            for nodes, _, _ in target_adverts:
                full_nodes = self._keyed_nodes(nodes, repeaters)
                if full_nodes:
                    terminals.add(full_nodes[0])
        if not terminals:
            return [], "AutoTrace: no resolvable target advert hop"
        options = {}
        # Bounded search: fewest hops first, then strongest weakest link.
        # Bound work on dense regional meshes: graph query is read-only, but
        # a naive all-simple-path search would block the command event loop.
        for first, first_count in sorted(
            first_hops.items(), key=lambda pair: -pair[1]
        )[:25]:
            heap = [(1, -first_count, (first,))]
            expanded = 0
            while heap and expanded < 2000:
                hops, neg_support, route = heapq.heappop(heap)
                expanded += 1
                node = route[-1]
                if node in terminals:
                    encoded = self._encode_graph_route(route, repeaters)
                    if encoded:
                        options[encoded] = max(
                            options.get(encoded, 0), -neg_support
                        )
                    continue
                if hops >= self.max_hops:
                    continue
                for neighbor, count in sorted(
                    adjacency.get(node, {}).items(),
                    key=lambda pair: -pair[1]
                )[:30]:
                    if neighbor in route:
                        continue
                    heapq.heappush(heap, (
                        hops + 1, -min(-neg_support, count), route + (neighbor,),
                    ))
        if not options:
            return [], "AutoTrace: graph has no unambiguous path"
        ranked = sorted(
            options.items(), key=lambda x: (len(x[0]), -x[1], x[0])
        )[:3]
        return [(route, f"{target_name} [graph]") for route, _ in ranked], None

    def _proposals(self, message, target=None):
        with self.bot.db_manager.connection() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if not {"observed_paths", "complete_contact_tracking"} <= tables:
                return [], "AutoTrace: observed_paths DB unavailable"
            contacts = self._contacts(conn)
            source_key = self._identity(message)
            if not source_key:
                return [], "AutoTrace: sender public key required"
            repeater_keys = self._repeater_keys(contacts)
            if not repeater_keys:
                return [], "AutoTrace: no known repeater identities"
            # The incoming command is direct evidence of this requester's
            # sender->bot RF path, even when the companion never advertises.
            # Only if it lacks a usable multi-hop route do we try advertised
            # paths bound to the requester's full public key.
            current, current_status = self._incoming_dm_route(message)
            sources = [(current, None, 1)] if current is not None else []
            if not sources:
                sources = self._observed(conn, source_key)
            if target is None:
                # 'autotrace paths' without a target shows raw inbound paths,
                # never claims these are a user-to-other-node recommendation.
                return [(hops, "inbound to bot") for hops, _, _ in sources[:3]], None
            tgt, error = self._resolve_target(target, contacts)
            if error:
                return [], error
            target_key = tgt[1]
            if target_key == source_key:
                return [], "AutoTrace: target is your own node"
            target_routes = self._observed(conn, target_key)
            if not target_routes and tgt[2] not in ("repeater", "roomserver"):
                return [], "AutoTrace: no target advert route"
            proposals, seen = [], set()
            target_rep = target_key if tgt[2] in ("repeater", "roomserver") else None
            # Stable recency preference: already ordered by last_seen, then count.
            for own, _, _ in sources[:60]:
                own_keys = self._keyed_nodes(own, repeater_keys)
                if own_keys is None:
                    continue
                for toward, _, _ in target_routes[:60]:
                    tgt_keys = self._keyed_nodes(toward, repeater_keys)
                    if tgt_keys is None:
                        continue
                    path = self._manual_route(
                        own_keys, tgt_keys, repeater_keys, target_rep
                    )
                    if path and path not in seen:
                        seen.add(path)
                        proposals.append((path, tgt[0]))
                        if len(proposals) >= 3:
                            return proposals, None
            if "mesh_connections" not in tables:
                return [], "AutoTrace: mesh_connections unavailable"
            return self._graph_candidates(
                conn, source_key, target_key, tgt[0], tgt[2],
                target_routes, contacts
            )

    async def _reply(self, message, lines):
        limit = min(158, self.get_max_message_length(message))
        body = ""
        for line in lines:
            candidate = body + ("\n" if body else "") + line
            if len(candidate.encode("utf-8")) > limit:
                break
            body = candidate
        return await self.send_response(message, body or "AutoTrace: no result")

    async def execute(self, message: MeshMessage) -> bool:
        if not self.enabled or not message.is_dm:
            return True
        _trigger, raw = self.split_trigger_and_args(message.content or "")
        tokens = raw.strip().split()
        action = ""
        if tokens and tokens[-1].lower() in ("paths", "1", "2", "3"):
            action = tokens.pop().lower()
        query = " ".join(tokens).strip() or None
        if query is not None and len(query) > 70:
            return await self._reply(message, [self.get_help_text()])
        if action in ("1", "2", "3") and not query:
            return await self._reply(message, [self.get_help_text()])
        try:
            proposals, error = self._proposals(message, target=query)
        except (sqlite3.Error, OSError, AttributeError, ValueError) as exc:
            self.logger.warning("autotrace route lookup error: %s", exc)
            return await self._reply(message, ["AutoTrace: history unavailable"])
        if error:
            return await self._reply(message, [error])
        if not proposals:
            return await self._reply(message, ["AutoTrace: no route evidence"])
        if query is None:
            lines = ["Your observed route(s) to bot:"]
            lines.extend(f"{i}. {','.join(p)}" for i, (p, _) in
                         enumerate(proposals, 1))
            return await self._reply(message, lines)
        if action == "paths":
            lines = ["Candidate routes (unverified):"]
            lines.extend(f"{i}. {','.join(p)}" for i, (p, _) in
                         enumerate(proposals, 1))
            return await self._reply(message, lines)
        index = int(action) - 1 if action else 0
        if index >= len(proposals):
            return await self._reply(message, ["AutoTrace: selected route unavailable"])
        key = self._identity(message)
        if key is None:
            return await self._reply(message, ["AutoTrace: sender key required"])
        now = time.monotonic()
        global_wait = self.global_seconds - (now - self._last_global)
        user_wait = self.per_user_seconds - (
            now - self._last_user.get(key, float("-inf"))
        )
        if global_wait > 0:
            return await self._reply(message, ["AutoTrace: cooldown " + wait_label(global_wait)])
        if user_wait > 0:
            return await self._reply(message, ["AutoTrace: cooldown " + wait_label(user_wait)])
        self._last_global = now
        self._last_user[key] = now
        route, name = proposals[index]
        # Manual user-side execution. We deliberately DO NOT invoke run_trace
        # or radio.commands.send_trace anywhere in this plugin.
        return await self._reply(message, [
            f"To {name[:18]}: candidate only",
            "Your radio: tracer " + ",".join(route),
            "Reverse links untested; no bot probe."
        ])
