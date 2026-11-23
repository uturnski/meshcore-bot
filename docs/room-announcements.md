# Room Server announcements

The optional `RoomAnnouncementsService` sends **outbound announcements only** to one or more MeshCore Room Servers through the same connected companion radio that the bot already uses. It does not bridge traffic, respond to room commands, or create a second radio connection. The normal `[Scheduled_Messages]` schedule remains independent.

## Configuration

Add to the local `config.ini` on the bot host (the example in `config.ini.example` is disabled by default):

```ini
[Room_Announcements]
enabled = true
room_servers = Hamilton Room
password = hello
schedule_minute = 15
message = Hamilton MeshCore users: Join #public-1 for local Hamilton-area communications.
login_timeout_seconds = 60
ack_timeout_seconds = 25
```

- **room_servers**: Comma-separated, case-insensitive **exact contact advertisement names** or **full 64-hex-character public keys**. Each must resolve to exactly one Room Server (contact type 3). If two rooms share a name, use their public keys. No wildcard, substring, or "all rooms" mode exists.
- **password**: Common login password for the listed rooms; defaults to the usual guest password `hello`. Guest posting requires that the Room Server still allows write access to that password. Put non-default passwords only in your local configuration; do not commit them.
- **schedule_minute**: 0–59, default 15. Fires once per server at that minute of **each hour**, according to `[Bot] timezone`. The publisher checks at five-second intervals during that minute. There is no backlog/catch-up after that minute.
- **message**: One line of static text or the documented network-statistics placeholders. The **rendered** result must be 1–150 UTF-8 bytes. Static overlength messages are rejected when the plugin loads; an oversized dynamic result is logged and skipped for that hour (never truncated or split). The raw template may be up to 2,048 UTF-8 bytes.
- **login_timeout_seconds / ack_timeout_seconds**: Bounds for remote login and message acknowledgement (60/25 seconds by default).

Before enabling the plugin, make sure the bot's companion device already has each Room Server in its contacts. Learn/add the room contact through a companion client before running the bot, or use the public key of a contact it already knows.

To retain the existing `Public` announcement, keep the existing independent job:

```ini
[Scheduled_Messages]
15 * * * * = Public:Hamilton MeshCore users: Join #public-1 for local Hamilton-area communications.
```

The Room Server service does **not** create this channel job automatically. If you already have a channel announcement scheduled at :15, leave it alone to avoid duplicates.

## Dynamic network statistics

Use one of the supported placeholders in `message` to read the bot's existing `complete_contact_tracking` database **at each scheduled hour**:

| Placeholder | Value |
| --- | --- |
| `{total_contacts}` | All contacts ever heard by this bot |
| `{total_repeaters}` | Contacts currently classified as repeaters |
| `{total_companions}` | Contacts currently classified as companions |
| `{new_repeaters_7d}` | Repeaters first heard in the last rolling seven days |
| `{new_companions_7d}` | Companions first heard in the last rolling seven days |
| `{recent_activity_24h}` | Contacts last heard in the last rolling 24 hours (all roles) |

The counts use the locally stored contact database, **not** all nodes currently online in the global mesh. The seven-day and 24-hour windows use the bot host's local clock, matching the timestamps recorded by the repeater manager. Each scheduled hour gets a single database snapshot shared across all configured rooms.

Example:

```ini
[Room_Announcements]
enabled = true
room_servers = Hamilton Room
password = hello
schedule_minute = 00
message = Network: {total_contacts} total ({total_repeaters} repeaters, {total_companions} companions). {new_repeaters_7d} new repeaters, {new_companions_7d} new companions in last 7d. {recent_activity_24h} active in 24h.
```

Keep the entire `message = ...` value on **one physical line**. Only the six placeholders listed above are accepted; unknown names, attribute/index access, conversion flags, and format modifiers are rejected at startup. Network stats are not populated with guessed zeros when the DB is unavailable: the service logs the error and skips that hourly announcement. An oversized *rendered* message is also skipped. In both cases no hourly transmission is claimed, and a new render is attempted at the next scheduled hour.

## Publish now with an admin DM

An administrator can trigger the same configured Room Server message **immediately**, without waiting for `schedule_minute`. Send a direct message to the bot:

```text
announce.room
```

The spelling `annouce.room` is also supported. Apply the bot's normal command prefix if `[Bot] require_command_prefix = true` (for example, `!announce.room` when the configured prefix is `!`). The trigger accepts no arguments; it only publishes the message from `[Room_Announcements] message`, including current network-statistics placeholders, to every configured room.

**Authorization:** Only DMs from full public keys in `[Admin_ACL] admin_pubkeys` may invoke the command. Set up the sender's *companion public key* there (not the Room Server's key). This uses the bot's standard admin ACL and is **independent** of `[Announcements_Command] enabled`: the old channel announcements command can remain off. The Room Announcements **service itself must be running**.

The command replies privately with counts of `ACK`, `unconfirmed`, `failed`, and `skipped` Room Servers. An ACK is from the addressed Room Server, not all readers. A failed login or missing Room Server contact is counted as failed; a missing post ACK is unconfirmed and not automatically retried. Manual triggers have a default **60-second cooldown** (configurable as `manual_cooldown_seconds = 60`, range 30–3600) and cannot overlap each other or the scheduled job.

Manual sends outside the configured minute **do not consume or reset** hourly send claims. When an admin invokes the command during the scheduled minute, both paths share the same per-room persisted deduplication marker, so no room is sent the scheduled announcement twice in that hour. When the manually rendered message exceeds 150 UTF-8 bytes or the statistics database is unavailable, the command reports that it was skipped and does not send.

## What happens each hour

1. The plugin resolves optional network-statistics placeholders and verifies the **rendered message fits 150 UTF-8 bytes**; only then does it claim the hour/server in the bot's metadata database **before transmitting**. It skips an already-claimed hour, including after a service restart.
2. It resolves the configured target to one contact and checks Room Server type and complete public key.
3. It sends a MeshCore login request with the configured guest password and waits for `LOGIN_SUCCESS` for that room's six-byte key prefix (or `LOGIN_FAILED`). **No post is sent if login is not confirmed.**
4. It sends the announcement **once**, waits for the message ACK, and logs whether the ACK arrived. A missing ACK is **delivery unknown**, not proof that the room rejected the message. It never retries automatically in that hour.
5. It logs out of the room. Inbound messages from configured rooms are ignored by the bot's DM command dispatcher while the service is running.

The bot's database must be available for the per-hour deduplication claim; if metadata cannot be written, **the plugin skips transmission** rather than risking duplicate posts. There is no guarantee of end-to-end delivery or of every room participant having seen a post: an ACK confirms receipt by the addressed room, not by all its members.

## Deploy on Debian

After merging the feature branch into the branch deployed to Debian, update the bot installation using your normal Git deployment procedure. Add the configuration locally, then restart:

```bash
sudo systemctl restart meshcore-bot
sudo journalctl -u meshcore-bot -n 100 --no-pager | grep -i 'Room announcements'
```

Check the service logs around minute :15. Do not copy the repository's example file over your production `/etc/meshcore-bot/config.ini` because it would overwrite your local settings. For safety, start with **one** Room Server, verify its contact name and permission, then add more.
