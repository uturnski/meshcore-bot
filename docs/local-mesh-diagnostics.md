# Local MeshCore bot diagnostics

This public feature branch adds six **local, read-only diagnostic command plugins** and one **route-aware alternative to the built-in ping** to the upstream [`agessaman/meshcore-bot`](https://github.com/agessaman/meshcore-bot) bot. These commands fit in the normal bot plugin loader, require no additional dependencies, and run on Debian under `/opt/meshcore-bot` when `[Bot] local_dir_path = /opt/meshcore-bot/local`.

**Deployment files:** `local/commands/{neighborlist,heardme,linktrend,mypaths,traffic,airtime}.py`, `modules/commands/alternatives/ping_diagnostics.py` · [small config example](examples/mesh-diagnostics.ini) · [full sanitized config template](../config.ini.diagnostics). No real operator credentials, identity keys, network addresses or coordinates should be committed.

## Supported commands

| Command | Who can use it | Function and examples |
|---|---|---|
| `!neighborlist [page]` | Admin DM | Paginated list of the most recently saved zero-hop neighbour discovery snapshot. Example `!neighborlist 2`. Does **not** initiate discovery. |
| `!heardme [1h\|6h\|24h\|7d]` | User DM | Latest bot-received DM SNR, history and path counts. Alias `!seenme`; example `!heardme 7d`. |
| `!linktrend [7d\|30d\|90d\|365d]` | User DM | Summarize SNR from stored direct-neighbour discovery cycles; example `!linktrend 30d`. |
| `!linktrend weak [window]` | User DM | Weakest neighbours from the most recent saved cycle; example `!linktrend weak`. |
| `!linktrend HEXKEY [window]` | User DM | Inspect one neighbour by hex public-key prefix; example `!linktrend A53E9609 7d`. |
| `!mypaths [24h\|7d\|30d]` | User DM | Top recorded inbound DM routes to the bot, with observation counts. Example `!mypaths 7d`. |
| `!traffic [15m\|1h\|6h\|24h]` | User DM | Observer packet counts and top packet types, using `packet_stream`. Example `!traffic 6h`. |
| `!airtime BYTES [sf=N bw=N cr=N pre=N tx=N]` | User DM | Estimate LoRa PHY airtime; optional `tx` multiplies assumed transmissions. Example `!airtime 80 sf=7 bw=62.5 cr=5 pre=32`. |
| `!ping` / `!pingplain` | User DM; short replies on channels | **New alternative plugin**: sender, SNR, hop count, and stored names of each incoming route repeater; `!pingplain` uses the original configurable response. |

All these plugins use a rate-limited, single-MeshCore-DM response (up to 158 UTF-8 bytes), and are DM-only; the `!neighborlist` plugin additionally requires administrator ACL authorization. Other commands are open to normal DM users unless the operator restricts them using bot-level controls.

## What the numbers actually measure

### `!heardme` / `!mypaths`

Upstream `StatsCommand` records received messages in `message_stats(timestamp, sender_id, is_dm, snr, hops, path, ...)`. These plugins **do not** measure sender-to-every-repeater reachability, loss, or successful delivery rate. For multi-hop traffic, SNR describes reception **at the bot's radio from the final hop**. `!mypaths` counts only stored DM rows with parseable hex route strings; rows that say only "Routed through N hops" provide insufficient path evidence.

**Privacy limitation:** Upstream `message_stats.sender_id` is a *contact display label* rather than a full authenticated public key. Two contacts sharing a name can have mixed history. Changing a contact name can split its history. Users should not treat `!mypaths` / `!heardme` as cryptographically verified personal histories. `!mypaths` requires a DM and does not accept an arbitrary other username.

For `!mypaths`, store received history with `[Stats_Command] collect_stats = true` and `track_all_messages = true`. `anonymize_users = true` is supported for lookups: the plugin uses the upstream hash of the display label. History availability depends on `[Stats_Command] data_retention_days` (in the uploaded full configuration it is **7 days**). Requests for 30-day history cannot return records already pruned; raise retention **only if you accept the privacy/storage tradeoff**.

### `!linktrend` / `!neighborlist`

These query `neighbor_observations`, saved by `[PacketCapture] neighbors_enabled = true` when the radio completes zero-hop discovery. Each row is an actual direct neighbour reception measurement recorded in a *discrete discovery cycle*, not a continuous signal trace. A missing row for a neighbour **does not prove an outage**. History requires several discovery cycles; scheduled cycles are normally separated by 12 hours or more. A radio change means older measurements from another self-public-key are excluded by `!linktrend`.

`!neighborlist` is admin-only; the separate built-in `!neighbors` command initiates RF discovery and is expensive. `!linktrend` itself performs no RF discovery.

### `!traffic`

Reads `packet_stream` rows with `type = 'packet'` and UNIX-second timestamps, counting **packets observed at this particular receiver**, not unique mesh-wide transmissions, total airtime, congestion, actual collision count, or delivery rate. Traffic levels depend on antenna/coverage and whether the web-viewer integration is writing rows. When the denormalized `payload_type_name` column exists, the plugin reports the top observed categories (older schemas omit the categories). The configured packet-stream retention may be as short as three days.

### `!airtime`

Uses the standard LoRa chirp-spread-spectrum airtime formula with **explicit header** and **PHY CRC enabled**, automatic low-data-rate optimization at symbol times of at least 16 ms. `BYTES` is the **entire LoRa PHY payload** (1–255), *not* the length of the MeshCore text. MeshCore framing, encryption and routing bytes must be included in the size for a realistic estimate. Example defaults `SF7 / BW62.5 kHz / CR4/5 / preamble32` suit the cited Canadian MeshCore profile, but you must verify your radio's current settings. `tx=` means an assumed number of transmissions, not measured hops or retransmissions. No local radio configuration is read and no RF packet is transmitted.

### `!ping` (named-hop alternative)

The upstream bot already owns the `ping` command. **Its local plugin loader
skips duplicate primary command names**, so simply creating
`local/commands/ping.py` cannot override it. This fork uses upstream's
`modules/commands/alternatives/` plugin mechanism and one config entry:

```ini
[Plugin_Overrides]
ping = ping_diagnostics

[Ping_Command]
enabled = true
```

With this enabled, a four-hop DM can look like the following **fictional example**:

```text
@[ExampleNode] 4 hops, SNR 5.5
a1b2c3: n/a
d4e5f6: Repeater Alpha
0a1b2c: Solar Relay Beta
f1e2d3: Example Relay 🅱️
```

The hop prefixes are obtained from the **current incoming message's**
correlated `routing_info.path_nodes` or its equivalent parsed route (one,
two, or three bytes per hop). Full public-key prefixes are matched against
stored repeater/roomserver names in `complete_contact_tracking` (or
`repeater_manager`). An unknown or ambiguous prefix resolves to `n/a`;
we never fabricate a route or guess between colliding prefixes. When a
route is unavailable, the reply contains only sender, hop count and SNR.
Channel pings also stay brief to conserve airtime.

This reply is constrained to one 158-byte MeshCore DM, with UTF-8-safe name
shortening when needed. **SNR is measured at the bot's receiver for the last
RF hop**, not separately for each repeater. It never contacts individual
repeaters or sends extra test packets. The built-in ping template from
`[Keywords] ping` remains available through `!pingplain`.

## Install on Debian

On a source checkout at `/opt/meshcore-bot`, switch to the feature branch (confirm you have no uncommitted edits):

```bash
cd /opt/meshcore-bot
sudo git fetch origin feature/local-mesh-diagnostics
sudo git switch feature/local-mesh-diagnostics
# The plugins are under /opt/meshcore-bot/local/commands/.
```

If your systemd service uses a different code checkout, copy the six local `.py` files to the active `[Bot] local_dir_path`/`commands/` directory **and** copy `modules/commands/alternatives/ping_diagnostics.py` into the matching installed core alternatives directory; consult `systemctl cat meshcore-bot` to determine the actual working directory and config path.

Merge relevant `[..._Command]` and `[Plugin_Overrides]` sections from [the short example](examples/mesh-diagnostics.ini) into the existing local `config.ini` or `/opt/meshcore-bot/local/config.ini`. **Don't overwrite the live bot config with `config.ini.diagnostics`: it is a deliberately disabled/sanitized template.** It contains placeholder/blank identities, locations, broker connection settings, and a nonworking radio connection type.

```bash
# Syntax check (adjust venv path if necessary)
/opt/meshcore-bot/venv/bin/python -m py_compile /opt/meshcore-bot/local/commands/{neighborlist,heardme,linktrend,mypaths,traffic,airtime}.py
sudo systemctl restart meshcore-bot
sudo journalctl -u meshcore-bot -n 300 --no-pager \
  | grep -Ei 'Successfully loaded local plugin|failed to load local plugin|linktrend|mypaths|traffic|airtime'
```

Try `!help`, `!help mypaths`, `!help traffic`, `!help airtime`, and each command as a DM. The upstream help dispatcher reads the plugins' `get_help_text()` automatically. Normal `!help` may truncate the long list; a **short fixed** `[Keywords] help` override is included in the examples, but it replaces the dynamic help menu and must be maintained manually.

## Test

```bash
python -m unittest discover -s tests -p test_local_diagnostics_extra.py -v
python -m unittest discover -s tests -p test_local_linktrend.py -v
python -m unittest discover -s tests -p test_ping_diagnostics.py -v
```

Tests are isolated, in-memory/temporary SQLite tests using stubs for the base command; they do not communicate with a radio. Passing tests **do not** establish live systemd integration.

## Security and scope

- Main production `config.ini`, local `config.ini`, databases, private keys and credentials remain gitignored.
- The root `config.ini.diagnostics` is intentionally a safe public template, with identifying configuration removed; it is **not** operational out of the box.
- Public branch code is unauthenticated for non-admin DM commands but uses normal upstream user and RF rate limiters and single-message responses.
- No GitHub CI secret, MQTT auth token, private network address or real GPS location is needed to use these commands.

### Canada (Southern Ontario) airtime preset

MeshCore Canada standard: 910.525 MHz / 62.5 kHz / SF7 / CR4/5. MeshCore
firmware currently uses 32 preamble symbols at SF7 (16 for SF9+).
Frequency does not affect the PHY airtime formula. Default example:
`[Airtime_Command] default_sf=7, default_bw_khz=62.5,
default_cr_denominator=5, default_preamble_symbols=32`.
Confirm `get radio` on the actual device; local networks can differ.

`!airtime` accepts fractional bandwidth such as `bw=62.5`. The
configured values only affect the estimator; they do NOT change the radio.
References: https://meshcore.ca/ and
https://github.com/meshcore-dev/MeshCore/blob/main/src/helpers/radiolib/RadioLibWrappers.h

### `!bots` directory and `!botsN` details

The directory is passive and uses previously stored/cached contact names.
**It is a list of unverified bot-name candidates, not a verified census.**
Any name containing "bot" (case-insensitively) can match, including false
positives such as "Robot"; bot software without "bot" in its name is missed.
The command never initiates discovery, queries a node or transmits extra
probe packets. Only its normal reply uses RF airtime.

Use the commands in `#test` (when allowed by `[Bots_Command] channels`) or by DM:

| Command | Purpose |
| --- | --- |
| `!bots` | Compact numbered names, **multiple entries per message** |
| `!bots 2` or `!bots page 2` | Page 2 of the names-only directory |
| `!bots1`, `!bots2`, `!bots3` | Details for directory entry 1, 2 or 3, **without a space** |
| `!bots info 2` or `!bots detail 2` | Equivalent detailed view, with a space |

Fictional directory example:

```text
Bots 1/1 (4)
1. BotAlpha
2. Community Bot
3. SolarBot
4. WeatherBot
name? unverified. !botsN info
```

Detailed `!bots2` output when local statistics are present:

```text
Bot 2/4 (unverified)
Community Bot
ID 012345abcdef | companion
Seen 3h ago | Ads 17
SNR -7.5dB | Hops 2
Stored local stats only
```

The detailed reply reads `complete_contact_tracking` first and, when needed,
`repeater_contacts`, to show stored public-key prefix, advertised device
role/type, last-heard age, advertisement count, SNR and last recorded hop count.
An absent value appears as `n/a`, not zero. The full public key and private
message contents are never displayed. `Seen 3h ago` does **not** mean the bot
is currently online. SNR/hops are existing contact metadata, not network-wide
measurements or evidence of bot functionality.

To minimize congestion, entries are numbered and names are abbreviated to the
available UTF-8 byte budget. Pages are dynamically packed (158 bytes max for
DMs; less in channels). The directory is sorted by observed name and full key;
**indices and page boundaries can change if contacts change**, so rerun
`!bots` before relying on `!botsN`. Multiple records with the same full key
are deduplicated. Prefixes shown in details are only labels, not cryptographic
proof of identity; names and advertised metadata can be spoofed.

Data sources are `complete_contact_tracking`, `repeater_contacts`, and the
already-loaded `meshcore.contacts` cache. Missing tables, databases or cache
produce a partial-data notice. No bot-detection flag or reliable online presence
indicator exists in the available upstream schema; no additional database
migration, message collection, broker or credential is needed.

Install `local/commands/bots.py`, merge `[Bots_Command]` from the
[small config example](examples/mesh-diagnostics.ini), and restart the bot.
A sample uses `channels = #test` (which must exist in the bot's channel
settings); set `channels =` for DM-only operation. To disable, set
`enabled = false`. Include `bots` in `[Keywords] help` if using the fixed
help override. Run regression tests:

```sh
python -m unittest tests.test_local_bots -v
```
