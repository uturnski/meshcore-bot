"""Local MeshCore Bot command: !neighborlist [page]

Place in local/commands/neighborlist.py and restart the bot.
Reports the most recently stored non-empty zero-hop discovery snapshot.
"""

from datetime import datetime, timezone
import math

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


PAGE_SIZE = 3  # Keep one response within the MeshCore 158-byte DM budget.


def _short_utf8(value: object, limit: int = 12) -> str:
    """Clean whitespace and limit a label by encoded bytes, not characters."""
    cleaned = " ".join(str(value or "").split())
    return cleaned.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _age_text(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _seconds_since(stamp: str) -> int:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    except (TypeError, ValueError, AttributeError):
        return 0


class NeighborListCommand(BaseCommand):
    name = "neighborlist"
    keywords = ["neighborlist"]
    description = "List neighbors from the last saved zero-hop discovery (!neighborlist [page])"
    short_description = "Page through stored zero-hop neighbors"
    usage = "neighborlist [page]"
    examples = ["neighborlist", "neighborlist 2"]
    category = "analytics"
    requires_dm = True
    cooldown_seconds = 3

    def requires_admin_access(self) -> bool:
        """Require a public key from [Admin_ACL] admin_pubkeys."""
        return True

    async def execute(self, message: MeshMessage) -> bool:
        parts = message.content.strip().removeprefix("!").split()
        if len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
            await self.send_response(message, "Usage: !neighborlist [page]")
            return True
        page = int(parts[1]) if len(parts) == 2 else 1
        if page < 1:
            await self.send_response(message, "Page must be 1 or greater")
            return True

        try:
            with self.bot.db_manager.connection() as conn:
                record = conn.execute(
                    "SELECT MAX(observed_at) FROM neighbor_observations"
                ).fetchone()
                stamp = record[0] if record else None
                if not stamp:
                    await self.send_response(
                        message, "No discovery results saved. Run !neighbors first."
                    )
                    return True

                records = conn.execute(
                    """
                    SELECT n.neighbor_public_key, n.snr, n.heard_secs_ago,
                           COALESCE((
                               SELECT c.name FROM complete_contact_tracking AS c
                               WHERE lower(c.public_key) = lower(n.neighbor_public_key)
                               ORDER BY c.last_heard DESC LIMIT 1
                           ), '') AS name
                      FROM neighbor_observations AS n
                     WHERE n.observed_at = ?
                     ORDER BY (n.snr IS NULL), n.snr DESC, n.neighbor_public_key
                    """,
                    (stamp,),
                ).fetchall()
        except Exception as exc:
            self.logger.error("neighborlist database query failed: %s", exc)
            await self.send_response(message, "No neighbor data available. Check database/logs.")
            return True

        total = len(records)
        if not total:
            await self.send_response(message, "Last discovery snapshot has no neighbors.")
            return True
        pages = math.ceil(total / PAGE_SIZE)
        if page > pages:
            await self.send_response(message, f"Only {pages} neighborlist pages available.")
            return True

        snapshot_age = _seconds_since(stamp)
        lines = [f"Neighbors {total} | {page}/{pages} | {_age_text(snapshot_age)} ago"]
        for idx in range((page - 1) * PAGE_SIZE, min(page * PAGE_SIZE, total)):
            record = records[idx]
            name = _short_utf8(record[3] or "unnamed", 12)
            key = str(record[0] or "????????")[:8].upper()
            snr = f"{record[1]:+.1f}dB" if isinstance(record[1], (int, float)) else "n/a"
            # heard_secs_ago is relative to the snapshot timestamp, not current time.
            heard_age = snapshot_age + max(0, int(record[2] or 0))
            lines.append(f"{idx + 1}. {name} {key} {snr} {_age_text(heard_age)}")

        output = "\n".join(lines)
        # Defensive guard; avoids a firmware-side truncation if names contain UTF-8.
        if len(output.encode("utf-8")) > self.get_max_message_length(message):
            self.logger.warning("neighborlist output exceeded DM budget: %s", output)
            await self.send_response(message, "Neighbor page too long; reduce display fields.")
        else:
            await self.send_response(message, output)
        return True