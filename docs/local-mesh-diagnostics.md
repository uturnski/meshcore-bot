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

## AutoTrace: historical reciprocal routing (experimental)

The local `!autotrace` command reuses upstream's `TraceCommand` reciprocal-path
helper and the shared `run_trace` engine; it does **not** implement a second radio
trace protocol. The feature is **disabled by default**.

| DM command | Effect |
|---|---|
| `!autotrace paths` | Up to 3 recent distinct received DM paths from SQLite; **no trace RF** |
| `!autotrace` or `!autotrace 1` | Trace the newest valid historical path, one attempt |
| `!autotrace 2` / `!autotrace 3` | Trace that ranked historical path if it exists |
| `!autotrace Dee` | Admin DM: resolve exactly one heard contact name and trace a stored path |
| `!autotrace a1b2c3` | Admin DM: resolve a **3-byte public-key prefix** to exactly one heard contact |
| `!autotrace Dee paths` | Admin DM: list available historical routes to named contact, no trace RF |

```ini
[Autotrace_Command]
enabled = false
per_user_cooldown_seconds = 900
global_cooldown_seconds = 60
maximum_hops = 5
max_route_age_hours = 24
```

**Airtime safeguards:** active commands require an authenticated 64-hex sender
public key and a DM; reserve the 15-minute per-user and 60-second global
cooldowns *before* any network send, including failed sends/timeouts; allow
one in-flight AutoTrace at a time; no fallback paths or automatic extra
attempts. Admin ACL members can bypass only the per-user interval, not the
global cooldown or concurrency limit. The shared trace runner now accepts
a per-call `max_attempts_override=1`; ordinary `!trace` still uses its configured
retry count. `!autotrace paths` bypasses these RF-specific cooldowns, but
still uses normal bot DM response rate-limiting.

**Target selection:** Names require an unambiguous full-name match (or an
unambiguous partial-name match). Six hexadecimal digits are interpreted as the
first **three bytes of the target's full public key**, not the trace route's
hash width. Ambiguous matches fail closed; do not infer that three-byte identity
hashes can be used directly in a one-byte MeshCore trace. Companion trace
candidates must come from that contact label's observed DM route, which
upstream stores without a verified public-key association. For repeaters the
bot finds an observed repeater hash in a received packet route and traces the
suffix of that route toward the repeater, rejecting collisions against all
known repeater public keys. Seeing a node advertise, without usable route
evidence, is not enough. Targeted lookups are **admin-only**; own-route
lookup remains available to normal DM users.

**Route safety:** the bot uses received `created_at` timestamps rather than
sender-supplied clocks. Reject stale, malformed, mixed-width, >5-hop or
currently unsupported 3-byte trace paths. Historical DM records are
indexed by contact *display name* in upstream `message_stats`, so the saved
route **is not cryptographically bound to the requesting public key** and
may be confused by duplicate/renamed labels. Access is restricted to
the requesting contact label; no arbitrary history lookup is supported.
Successful traces verify a repeater round-trip to the bot, **not delivery
to the companion**. Routes are reversed from sender-to-bot and made
reciprocal before passing to the shared runner. This feature does not
globally lock the existing manually invoked `!trace` command.

Testing: `python -m pytest tests/test_autotrace_local.py -q` (mock radio,
SQLite fixture). Production RF still requires a controlled field test.

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

### `!bots` passive responder directory and `!botsN` details

`!bots` discovers possible bot software by correlating **received PUBLIC**
`!ping`, `!path`, and `!test` requests with recognizable responses
on the same channel within a short interval. It also includes nodes
with an indicative `Bot` name. **All tags are heuristic and unverified.**
Messages to other users' encrypted DMs are not visible. This command NEVER
sends automated pings, scans, or probes the mesh. Its replies use normal RF airtime.

| Marker | Evidence |
| --- | --- |
| `+` | Observed replies to **two or more different** command types (ping/path/test) |
| `*` | Observed a reply to **one** of these commands |
| `?` | Name indicates `Bot`, but no matching command response observed |

Matching uses the `bot` token (e.g. `Bot Alpha`) or CamelCase names like
`WeatherBot`/`BotAlpha`. A name like `Abbot`, `Robot`, `Botany`, or `Bottom`
no longer qualifies on name alone.

Commands supported in your bot's configured `#test` channel or DM:

| Command | Meaning |
| --- | --- |
| `!bots` | Multiple numbered names in one reply, sorted `+`, `*`, then `?` |
| `!bots 2` / `!bots page 2` | Page 2 of the compact directory |
| `!bots1`, `!bots2` | Details for directory item #1 or #2 (no space) |
| `!bots info 2` / `!bots detail 2` | Equivalent detail request |

Illustrative output with fictional names:

```text
Bots 1/1 (4)
1. +RelayAssistant
2. *WeatherNode
3. ?BotAlpha
4. ?SolarBot
+=2 *=1 ?=name (unverified)
```

The detail reply includes stored advert count, last-heard age, SNR, hop count,
and the observed **reply count and types**, when enough data is available.
Example of `!bots1`:

```text
Bot 1/4 + RelayAssistant
ID 012345abcdef | companion
Seen 3h | SNR -7.5dB
Ads 17 | Hops 2
Reply 3x path,ping,test (1h)
Local only; unverified
```

### How responder evidence is collected

The existing upstream `[Stats_Command]` must record public messages:

```ini
[Stats_Command]
enabled = true
collect_stats = true
track_all_messages = true
anonymize_users = false

[Bots_Command]
enabled = true
channels = #test
reply_window_seconds = 45
evidence_retention_days = 30
```

`!bots` reads the newest 4,000 saved public messages from `message_stats` and
recognizes the default MeshCore `Pong!` response, this fork's
`@[requester] N hops, SNR ...` ping reply, the upstream `ack @[requester]`
test response, and structured path replies (`HEX: name` or `Path: ...`).
It only counts a reply if there is a preceding request of the **same** type
on that channel within `reply_window_seconds`. Where the response names a
requester, the requester must match. A new local SQLite table,
`bot_command_responses`, retains per-reply type/time evidence for the configured
retention period; repeated `!bots` reads do not double-count existing rows.
Custom answer formats may not be recognized. The limited saved-message window
can miss older observations; history starts only when collection is enabled.

Channel sender names are extracted from user-supplied text and **not**
cryptographically authenticated. Unrelated replies, spoofed sender names, or
responses to other concurrent requests can produce false positives; `+` is
stronger behavioural evidence, not proof of identity or software. Only link
a displayed responder name to a public key when exactly one stored contact
has that name. For ambiguous or uncatalogued names, the details show `ID n/a`.

Raw message contents are read locally for detection but not included in
responses; only the observed name, command type, time, and source DB row ID
are retained in the evidence table. Without `message_stats`, saved contacts
may still appear as `?`, but reply-based detection is unavailable.
`anonymize_users = true` prevents linking stored sender names to known contacts.
Correlations are local, not network-wide, and are not online-presence tests.

The directories remain limited to 158 UTF-8 bytes for DMs and smaller limits
for public channels. Names are truncated safely and list numbering can change
as contacts or observations change. Refresh `!bots` before selecting `!botsN`.

Install the updated `local/commands/bots.py`, merge the `[Bots_Command]`
settings from [the config example](examples/mesh-diagnostics.ini), and restart.
To disable, set `enabled = false`; for DM-only listing set `channels =`.
Include `bots` in `[Keywords] help` if you use a static help override.
Run isolated tests with:

```sh
python -m unittest tests.test_local_bots -v
```

## `!rneighbors`: manually selected remote repeater neighbours

This command **only targets repeaters you explicitly add** to the private
`[Rneighbors_Command] repeaters` allowlist. Nothing is auto-discovered, polled,
or queried in the background. Requests are restricted to authenticated
**administrator DMs**; the command requires the caller's full public key to be
in `[Admin_ACL] admin_pubkeys`.

Use the following in your **private** active `config.ini` or `local/config.ini`
(merge into existing sections; do not commit your actual public keys or secrets):

```ini
[Rneighbors_Command]
enabled = true
repeaters = rep1=YOUR_64_CHARACTER_REPEATER_PUBLIC_KEY
request_count = 12
```

### Commands

| Command | RF traffic | Behavior |
| --- | --- | --- |
| `!rneighbors` | No remote query | Show selected aliases and their public-key prefixes |
| `!rneighbors rep1` | No remote query | Display first page of cached remote neighbours |
| `!rneighbors rep1 2` | No remote query | Display cached results page 2 |
| `!rneighbors refresh rep1` | **Yes, one** request | Fetch first 12 entries for selected `rep1` |
| `!rneighbors refresh rep1 2` | **Yes, one** request | Fetch next 12 entries (`offset=12`) and merge into cache |

`request_count` is clamped to 1–24; the remote query uses
`meshcore.commands.req_neighbours_sync()` with the selected exact 64-hex key,
4-byte neighbour prefixes, a 12-second library timeout, and an 18-second
outer timeout. A timeout, unsupported firmware, or repeater access restriction
returns an error and leaves cached results untouched. The query uses **binary
neighbour requests**, not remote text CLI scraping. It does **not** implicitly
perform a guest/admin login; firmware support and permissions control whether
the remote node replies.

Each response contains the repeater's neighbour prefixes, SNR and relative
last-heard ages. Entries display optional names from
`complete_contact_tracking` **only when the prefix identifies exactly one
stored public key**; unknown/ambiguous prefixes show `-`. Reply lengths are
bounded to 158 UTF-8 bytes with three entries per cached page.

Results are in the bot's local SQLite database (`rneighbors_cache`) rather than
a network-wide map. Partial results are clearly labelled as `saved/total`.
The cached age is measured since retrieval; last-heard values were measured
by the *remote repeater*, not by your bot. Observations are directional and
may be stale. Each page fetch spends actual LoRa airtime, so **only request
more pages when you need them**.

The plugin is in `local/commands/rneighbors.py`. It does not replace the
existing `!neighborlist` command, which shows **this bot's own** saved
zero-hop neighbour discovery.

Before use, verify your installed MeshCore Python library supports
`req_neighbours_sync`. It exists in the upstream
[meshcore_py binary command client](https://github.com/meshcore-dev/meshcore_py/blob/main/src/meshcore/commands/binary.py).
Copy the plugin to your active `local/commands/` directory, configure the
allowlist privately, restart your Debian bot and send `!rneighbors` by admin DM.
It is disabled by default in published example settings.

Isolated tests: `python -m unittest tests.test_local_rneighbors -v`.
