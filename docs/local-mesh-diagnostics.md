# Local diagnostic commands for MeshCore Bot

This branch adds three independently loadable commands under `local/commands/`:

| Command | Audience | Example | Purpose |
| --- | --- | --- | --- |
| `!neighborlist [page]` | Admin only, DM | `!neighborlist 2` | Paginate the most recent stored zero-hop neighbour discovery snapshot (three entries per DM). |
| `!heardme [1h\|6h\|24h\|7d]` (alias `!seenme`) | Community users, DM | `!heardme 7d` | Show current DM SNR/hop count and *recorded* past DM observations. |
| `!linktrend [window]` | Community users, DM | `!linktrend 30d` | Summarize observed direct-neighbour SNR measurements. |
| `!linktrend weak [window]` | Community users, DM | `!linktrend weak` | Show weakest nodes in the latest discovery cycle. |
| `!linktrend HEXKEY [window]` | Community users, DM | `!linktrend A53E9609 7d` | Compare recorded measurements of one neighbor by public-key prefix. |

LinkTrend windows are `7d`, `30d` (default), `90d` and `365d`. All commands reply in a single short DM to minimize RF airtime. `!linktrend` is **read-only**: it does not start discovery, ping a radio or query live reception. A neighbour missing from a discovery cycle is **not** proved offline.

## Installing on Debian

The custom command files must be present in the directory the active config uses for `[Bot] local_dir_path`, usually `/opt/meshcore-bot/local/commands` in a source checkout.

```sh
# On a checkout of this feature branch:
sudo install -d /opt/meshcore-bot/local/commands
sudo install -m 644 local/commands/neighborlist.py local/commands/heardme.py local/commands/linktrend.py /opt/meshcore-bot/local/commands/
sudo systemctl restart meshcore-bot
sudo journalctl -u meshcore-bot -n 200 --no-pager | grep -Ei 'loaded local plugin|neighborlist|heardme|linktrend'
```

If your working checkout *is* `/opt/meshcore-bot`, no file copies are needed after a checkout or pull: the files will already be in the correct location. Check systemd's `ExecStart` and the loaded config path before touching a working installation.

The [sanitized configuration example](examples/mesh-diagnostics.ini) contains optional command settings, including a short fixed help menu. Merge the relevant options into the existing production config, ideally `/opt/meshcore-bot/local/config.ini`. Do not replace existing configuration with the example.

`!help heardme`, `!help linktrend` and `!help neighborlist` are handled by the upstream help dispatcher when the plugins load. Generic `!help` dynamically lists loaded commands, but might truncate the list to fit the packet payload. The optional `[Keywords] help` override in the example fixes this by substituting a small list; it also hides other commands from that menu, so maintain it manually.

## Source data / limits

- `!neighborlist`: `neighbor_observations` plus known `complete_contact_tracking` names; requires stored zero-hop data, and administrator access. Run the existing `!neighbors` command as admin for a fresh snapshot.
- `!heardme`: `message_stats` and optionally `path_stats`; requires `[Stats_Command] collect_stats = true` and `track_all_messages = true`. History uses the sender label stored by upstream StatsCommand rather than a cryptographically unique key; counts can combine identities sharing a label. SNR of multi-hop DMs is the final-hop RF signal received by the bot.
- `!linktrend`: `neighbor_observations` created by migration 22, enabled through `[PacketCapture] neighbors_enabled = true`. Observations are normally stored once per neighbour per completed discovery cycle, and are pruned according to `[Data_Retention] neighbor_observations_retention_days`. The command uses the radio public key in the newest observation, so data from other bot radios is excluded. A new installation may have only a single cycle, making trend calculations unavailable until later cycles.
- All output obeys the 158-byte DM body budget and contains no private credentials. The public-key prefixes reported are derived from the bot's observed mesh neighbours.

## Privacy / repository hygiene

This **public** fork tracks only selected safe plugin source files. The upstream `.gitignore` blocks `config.ini`, `local/config.ini`, and files under `local/commands` by default. This feature branch explicitly allows only `neighborlist.py`, `heardme.py`, and `linktrend.py` to be version-controlled. Keep site-specific addresses, email recipients, access tokens, passwords and actual administrator public keys out of the repository (even full public keys may be identifying). Use local ignored config files for deployment.

No changes to the original upstream bot or your production radio settings are required.
