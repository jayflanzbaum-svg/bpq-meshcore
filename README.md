# bpq-meshcore

Use your BPQ32 / LinBPQ node from **MeshCore**: from the MeshCore phone app,
a T-Deck, or any MeshCore companion. **Direct messages** to the bridge radio
(private per user, identified by public key), or posts on a private channel,
are relayed into your node over telnet, as the sender's own callsign. Users land at
the node prompt and get exactly what a telnet user gets: `BBS`, `CHAT`,
your node applications, and the BBS's own commands once inside it.

```
 phone / T-Deck  ──LoRa──►  MeshCore radio on USB  ──►  bpq_meshcore.py  ──telnet──►  BPQ node
 (private channel)          (USB Serial Companion)                         (telnet TCPPORT)  
```

There is no new command set to learn. Each mesh message is one line typed at
the node. The bridge adds one thing: replies longer than 4 mesh messages are
paged, and `M` sends the next page.

## What you need

- A MeshCore radio that has a **USB Serial Companion** build, for example a
  Heltec V3/V4, RAK4631, or LilyGo T-Beam. The BLE companion build will not
  work, because it does not talk over USB.
- Python 3.10+ on the BPQ machine. Tested on Windows with BPQ32 6.0.25;
  LinBPQ should work the same.
- A telnet port in BPQ (`DRIVER=Telnet`, `TCPPORT=`).

## 1. Flash the radio

Use the MeshCore web flasher (<https://flasher.meshcore.co.uk>, in Chrome or
Edge), or `esptool` with the `*_companion_radio_usb-*-merged.bin` file for your
board from <https://github.com/meshcore-dev/MeshCore/releases>:

```
py -m pip install esptool
py -m esptool --port COM9 erase-flash
py -m esptool --port COM9 write-flash 0x0 heltec_v4_companion_radio_usb-v1.18.0-xxxxxxx-merged.bin
```

Pick the right variant. The Heltec V4, for example, comes as `heltec_v4` (2 MB
PSRAM) and `heltec_v4_r8` (8 MB PSRAM, different power amplifier).
`py -m esptool --port COM9 flash-id` shows which you have. To keep the old
firmware, `read-flash 0 ALL backup.bin` first.

## 2. Name it and create the private channel

```
py -m pip install -r requirements.txt
py tools\setup_radio.py COM9 "N0CALL BBS"
```

This sets the node name and the US preset (910.525 MHz / 62.5 kHz / SF7 /
CR5). Use `--no-radio`, or `--freq/--bw/--sf/--cr`, elsewhere. It then puts a
private channel in slot 1 with a **random** key, and writes the key plus a QR
code to `channel-secrets\`. Scan the QR code in the MeshCore app to join.

## 3. Give each mesh user a telnet login

Add one line per user to your telnet port's `CONFIG` block in `bpq32.cfg`, and
restart BPQ:

```
       USER=n0call-mesh,SomeLongRandomPassword,N0CALL,""
```

Leave the fourth field empty (`""`) so the user lands at the node prompt.
Don't add `SYSOP`. Use the user's real callsign in the third field: it is who
the BBS thinks they are, so their mail is theirs.

## 4. Configure and run

```
py bpq_meshcore.py        # first run writes meshcore_config.json and exits
```

Edit `meshcore_config.json`:

```json
{
  "radio":   {"port": "COM9", "baud": 115200},
  "channel": {"index": 1},
  "bbs":     {"host": "127.0.0.1", "port": 8100},
  "users": {
    "N0CALL-Phone": {"callsign": "N0CALL", "telnet_user": "n0call-mesh",
                     "telnet_password": "SomeLongRandomPassword"}
  }
}
```

- `bbs.port` is your telnet `TCPPORT`.
- Each key under `users` is the sender's **MeshCore name**, exactly as it
  appears before the colon in their channel messages.
- `"pubkey"` (the first 12 or more hex characters of their public key) lets
  that user **DM** the bridge. This is the recommended way in: a DM is private
  to that user, and only the holder of the key can send it.
- Messages from names or keys not on the list are ignored and logged, unless
  guests are on (below), which answers any DM. An unknown DM is logged as `ignored DM ... from unlisted key 05df63593866`,
  which is the easiest way to get a new user's key: they DM `hi`, you copy
  the key from the log.
- Set `"channel": {"index": null}` to accept DMs only.

### Adding a DM user

1. They send a **flood advert** from their MeshCore device. The bridge radio
   can only decrypt DMs from a contact it has heard advertise.
2. They add your bridge as a contact. It floods its own advert at start and
   every `adverts.flood_every_h` hours (default 12).
3. They DM `hi`. You copy their key from the log into their `"pubkey"`, give
   them a telnet `USER=` line, and restart the bridge (and BPQ, for the new
   USER line).

### Guests and self-signup (`guests`)

With `"guests": {"enabled": true}`, any MeshCore radio that DMs the bridge
gets a guest menu, without a callsign or a node login:

```
N4SFL-8 Mesh Gateway - guest
WX=Weather|SPOTS=Band activity|RPTSRCH=Repeaters|DIRECTIONS=Directions
Ham? Send CALL <callsign> for more.
```

- **Guests never reach BPQ or RF.** The bridge connects them straight to the
  internet-only applications on their `CMDPORT`s (`guests.apps`, command ->
  port), sending `GUEST` + 6 hex of the radio's key as the callsign, the
  way BPQ sends a user's callsign. Each radio keeps its own zip, grid and so
  on. `BBS`, `CHAT`, `CONNECT` and `RMS` answer that they need a ham login.
- **`CALL N0XYZ`** asks for more. The callsign is checked against the FCC
  database (`guests.verify_url`, callook.info; US licenses only), then every
  radio in `users` gets a DM and `guests.notify_project` (a notify-hub
  project, folder in `guests.notify_path`) a text. A failed lookup still
  goes to you, marked not verified.
- **From any radio in `users`:** `APPROVE N0XYZ`, `DENY N0XYZ`, `PENDING`.
  An approved member also gets `guests.member_apps` (default the two AI
  apps), under their own callsign. Members are kept in `mesh_members.json`
  (gitignored).
- Full node and BBS access still needs a telnet `USER=` line and an entry in
  `users`, as above.
- The radio can only answer contacts, so the bridge turns on auto-add
  contacts when guests are on. A new user floods one advert first.

### What the mesh sees (`display`, all optional)

```json
"display": {
  "node_prefix": "N0CALL",
  "command_sep": "|",
  "welcome": ["Welcome to the N0CALL-8 Mesh to BPQ Gateway.", "[?=Commands, BYE=Log off]"],
  "compact_lists": true,
  "bbs_prompt": "BBS>",
  "menu_on_return": true,
  "app_names": {"SPOTS": "Live Spots"}
}
```

- `node_prefix` replaces BPQ's `ALIAS:CALL-8} ` in front of node replies. Use
  `""` to remove it.
- `command_sep` joins the `?` command list, so 17 commands fit in one
  message: `N0CALL BBS|CHAT|…|MHEARD`.
- `welcome` is sent instead of the telnet port's CTEXT.
- `compact_lists` shortens BBS message lists (`L`, `LM`, `LL`, ...) from
  `5418 16-Nov PN 1298 N0CALL @N0CALL N1ABC test` to
  `MSG#5418 NEW from N1ABC RE: test`. NEW marks unread personal mail;
  bulletins end with their category, e.g. `[WSPR@WW]`.
- `bbs_prompt` replaces the BBS's `de CALL>` prompt.
- `menu_on_return`: when BPQ says `Returned to Node` (after `NODE` in the
  BBS or `Q` in an app) the bridge drops that line and asks the node for
  `?`, so the user lands back at the node with its command list.
- `app_names` turns BPQ's `*** Connected to SPOTS` into
  `*Connected to Live Spots*`; apps not listed keep their command name.
- In the BBS, `R5418` / `K5418` are sent as `R 5418` / `K 5418`, which is
  what BPQMail wants.
- `NODE` typed at the node itself (the app already ended, or the session
  timed out) shows the node's command list. BPQ would otherwise read it as
  `NODES` and list every node heard.
- `join_after` drops a blank line that follows a line starting with one of
  these, e.g. `["Source:"]` so a WX report's forecast follows its header.
- `telnet_only` (its own block, not under `display`): commands that move
  files or speak a binary protocol (`YAPP` in the BBS, `RMS` at the node,
  `GRIB`/`YAPP`/`FILE`/`DL` in WX) get `Use Telnet or RF to use this
  feature.` instead of being passed on. Keyed by `NODE`, `BBS` or an app's
  command name, matched on the first word.

These change only what the mesh sees. Telnet and RF users are unaffected. A
line too long for one message carries on in the next one, which starts with
`(cont)`.

Then:

```
py tools\test_login.py    # telnet check for every user, no radio needed
py bpq_meshcore.py        # run the bridge
```

Only one program can have the radio's COM port open. Close meshcore-cli or
anything else using it first. If the radio or BPQ goes away, the bridge
retries every 30 seconds.

## Using it

Post on the private channel:

- `hi` connects you and shows the node's welcome text. A command such as
  `BBS` connects you and runs it straight away.
- At the node: `BBS`, `CHAT`, `?`, your applications, `BYE`.
- Inside the BBS: `L`, `LM`, `R 123`, `SP N0XYZ`, then the title, body lines
  and `/EX`. `B` leaves the BBS.
- `M` pages on when a reply ended with `[M=more, n pages left]` (one page = one reply of up to `chunks_per_reply` messages).
- A bare `L` in the BBS is sent as `LL 10` (the last 10 messages), because
  plain `L` lists everything you haven't listed yet, often hundreds of lines.
  Set `bbs.bare_list` to another command, or to `null` to send `L` as typed.

Output is forwarded whenever it arrives, so a slow application still gets its
answer through. Lines are packed into mesh messages of up to 118 characters,
2.5 s apart, 4 per page. The FBB SID line and ASCII-art banners are dropped to
save airtime. A session closes after 10 minutes of silence. All of these are
settings in `meshcore_config.json` (`paging`, `bbs.quiet_s`,
`bbs.idle_close_s`).

One command never sends more than `paging.max_pages` pages (default 5, which
is 20 messages). The rest is dropped and the last message reads
`[List cut at 20 msgs. Try LL 10 or L 5590-5595]`. Set it to `0` for no cap.

## Security and licensing

- **The channel key is the access control.** MeshCore channel messages carry
  the sender's name as plain text, and anyone with the key can type any name.
  Share the QR code only with people you would give a node login to. If it
  leaks, run `setup_radio.py` again to make a new key.
- Telnet users can `CONNECT` out on your RF ports, as with any telnet login.
  In the US, invite **licensed** hams only, under their own callsign. A post
  they make can be forwarded over amateur bands under your station's control.
- MeshCore encrypts everything, so run it as a Part 15 device on 902–928 MHz.
  Do not run it under Part 97, because §97.113(a)(4) bars obscured messages.
  The bridge PC is the boundary between the encrypted Part 15 side and the
  plain-text amateur side. Check your own country's rules.

## Roadmap

- Approved members get their own node login without a BPQ restart.
- A one-time new-mail notice for listed users.

## Files

| File | What |
|---|---|
| `bpq_meshcore.py` | the bridge |
| `tools/setup_radio.py` | name, radio preset, private channel, join QR |
| `tools/test_login.py` | telnet login check, no radio |
| `meshcore_config.json` | your settings and passwords (gitignored) |
| `mesh_members.json` | guest signups and approvals (gitignored) |
| `channel-secrets/` | channel key and QR (gitignored) |
| `logs/bpq_meshcore.log` | rotating log (gitignored) |

MIT licensed. Written for the N4SFL-8 node by N8FLA.
