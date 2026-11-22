# Room Server announcements

The optional \`RoomAnnouncementsService\` sends **outbound announcements only** to one or more MeshCore Room Servers through the same connected companion radio that the bot already uses. It does not bridge traffic, respond to room commands, or create a second radio connection. The normal \`[Scheduled_Messages]\` schedule remains independent.

## Configuration

Add to the local \`config.ini\` on the bot host (the example in \`config.ini.example\` is disabled by default):

\`\`\`ini
[Room_Announcements]
enabled = true
room_servers = Hamilton Room
password = hello
schedule_minute = 15
message = Hamilton MeshCore users: Join #public-1 for local Hamilton-area communications.
login_timeout_seconds = 60
ack_timeout_seconds = 25
\`\`\`

- **room_servers**: Comma-separated, case-insensitive **exact contact advertisement names** or **full 64-hex-character public keys**. Each must resolve to exactly one Room Server (contact type 3). If two rooms share a name, use their public keys. No wildcard, substring, or "all rooms" mode exists.
- **password**: Common login password for the listed rooms; defaults to the usual guest password \`hello\`. Guest posting requires that the Room Server still allows write access to that password. Put non-default passwords only in your local configuration; do not commit them.
- **schedule_minute**: 0–59, default 15. Fires once per server at that minute of **each hour**, according to \`[Bot] timezone\`. The publisher checks at five-second intervals during that minute. There is no backlog/catch-up after that minute.
- **message**: A single line of 1–150 UTF-8 bytes. Oversized messages are rejected instead of split into multiple RF packets.
- **login_timeout_seconds / ack_timeout_seconds**: Bounds for remote login and message acknowledgement (60/25 seconds by default).

Before enabling the plugin, make sure the bot's companion device already has each Room Server in its contacts. Learn/add the room contact through a companion client before running the bot, or use the public key of a contact it already knows.

To retain the existing \`Public\` announcement, keep the existing independent job:

\`\`\`ini
[Scheduled_Messages]
15 * * * * = Public:Hamilton MeshCore users: Join #public-1 for local Hamilton-area communications.
\`\`\`

The Room Server service does **not** create this channel job automatically. If you already have a channel announcement scheduled at :15, leave it alone to avoid duplicates.

## What happens each hour

1. The plugin claims the hour/server in the bot's metadata database **before transmitting**. It skips an already-claimed hour, including after a service restart.
2. It resolves the configured target to one contact and checks Room Server type and complete public key.
3. It sends a MeshCore login request with the configured guest password and waits for \`LOGIN_SUCCESS\` for that room's six-byte key prefix (or \`LOGIN_FAILED\`). **No post is sent if login is not confirmed.**
4. It sends the announcement **once**, waits for the message ACK, and logs whether the ACK arrived. A missing ACK is **delivery unknown**, not proof that the room rejected the message. It never retries automatically in that hour.
5. It logs out of the room. Inbound messages from configured rooms are ignored by the bot's DM command dispatcher while the service is running.

The bot's database must be available for the per-hour deduplication claim; if metadata cannot be written, **the plugin skips transmission** rather than risking duplicate posts. There is no guarantee of end-to-end delivery or of every room participant having seen a post: an ACK confirms receipt by the addressed room, not by all its members.

## Deploy on Debian

After merging the feature branch into the branch deployed to Debian, update the bot installation using your normal Git deployment procedure. Add the configuration locally, then restart:

\`\`\`bash
sudo systemctl restart meshcore-bot
sudo journalctl -u meshcore-bot -n 100 --no-pager | grep -i 'Room announcements'
\`\`\`

Check the service logs around minute :15. Do not copy the repository's example file over your production \`/etc/meshcore-bot/config.ini\` because it would overwrite your local settings. For safety, start with **one** Room Server, verify its contact name and permission, then add more.
