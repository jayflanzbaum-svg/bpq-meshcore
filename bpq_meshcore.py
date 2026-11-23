"""
bpq_meshcore.py - MeshCore <-> BPQ32 / LinBPQ node bridge

A MeshCore radio on USB (USB Serial Companion firmware) listens on a private
channel. Each allowed sender gets a telnet session into BPQ (port 8100),
logged in as their own callsign, landing at the NODE prompt - exactly what a
telnet user gets. From there the normal node commands work: BBS, CHAT, SPOTS,
OFFGRIDAI, OPENAI, RPTSRCH, WX, DIRECTIONS, and the BBS's own commands once
inside it. The bridge adds no commands of its own except one:

  M   when the last output was longer than one reply, sends the next page.
      (Only intercepted while pages are waiting; otherwise M goes to BPQ.)

Every mesh message is sent to BPQ as one line. Output is forwarded whenever
it arrives (an AI answer that takes 20 s still gets delivered), once BPQ has
been quiet for quiet_s.

Two ways in, both checked against the "users" allow-list:

  Direct message (recommended): the user DMs this radio. Matched by the
  sender's PUBLIC KEY ("pubkey" in their user entry), which only the holder
  of the private key can produce, and private to that user. Replies go back
  as DMs with ACK and retries.

  Private channel: matched by the sender NAME before the colon. Channel names
  are NOT authenticated and every key holder sees every session, so use it
  for the sysop only, or set channel.index to null to turn it off.

The radio has to know a DM sender as a contact before it can decrypt their
messages, so a new user sends one advert first. The bridge floods its own
advert at start and every adverts.flood_every_h hours so users can add it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from meshcore import EventType, MeshCore

from radio_web import RadioWeb

HERE = Path(__file__).resolve().parent
# Optional first argument: another config file (bench tests).
CONFIG_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "meshcore_config.json"

DEFAULT_CONFIG = {
    "radio": {"port": "COM9", "baud": 115200},   # Linux: "/dev/ttyACM0"
    "channel": {"index": 1},
    "bbs": {
        "host": "127.0.0.1",
        "port": 8100,
        "login_timeout_s": 15,
        "quiet_s": 1.5,
        "idle_close_s": 600,
        # A bare "L" in the BBS lists every message; send this instead.
        # null leaves "L" alone.
        "bare_list": "LL 10",
    },
    # max_pages: most pages one command may send (0 = no cap); the rest is
    #            dropped with a hint to ask for less.
    # ack_wait_s / dm_attempts: how long one DM waits for its ACK and how many
    # tries (the last one flooded). Multi-hop ACKs often get lost even when the
    # message arrived, and the radio's own suggested wait (x3 tries) held each
    # part of a reply for 20-35 s.
    "paging": {"chunk_chars": 118, "chunks_per_reply": 4, "gap_s": 2.5,
               "ack_wait_s": 8, "dm_attempts": 3,
               "max_pages": 5},
    "adverts": {"flood_every_h": 12},
    # Local web page for managing the radio (the bridge holds the USB port,
    # so the official app cannot): http://127.0.0.1:<port>, this PC only.
    "web": {"enabled": True, "port": 8091},
    # Guests: any radio that DMs the bridge without being in "users". They
    # never reach BPQ or RF: the bridge connects them straight to the
    # internet-only apps on their BPQ CMDPORTs, as that radio's own user
    # (GUEST + 6 hex of its key, so each keeps its own zip, grid, ...).
    # Licensed hams can send CALL <callsign>: it is checked against the FCC
    # (verify_url), the sysop radios in "users" get a DM and `notify_project`
    # an alert, and APPROVE <call> from a sysop radio makes them a member,
    # who also gets member_apps under their own callsign.
    "guests": {
        "enabled": False,
        "host": "127.0.0.1",
        "apps": {"WX": 63051, "SPOTS": 63050, "RPTSRCH": 63052, "DIRECTIONS": 63053,
                 "OFFGRIDAI": 7374},
        "member_apps": {"OPENAI": 7373},
        # What a guest gets for BBS (they cannot log in to it).
        "bbs_info": ("The BBS is a ham radio mailbox: mail and bulletins that reach hams "
                     "nationwide, plus Winlink email. Hams: send MYCALL <callsign>."),
        "labels": {"WX": "Weather", "SPOTS": "Band activity", "RPTSRCH": "Repeaters",
                   "DIRECTIONS": "Directions", "OPENAI": "AI", "OFFGRIDAI": "Offgrid AI"},
        "title": "Mesh to BPQ Gateway",
        "verify_url": "https://callook.info/{call}/json",
        "notify_project": None,      # notify-hub project for signup alerts
        "notify_path": None,         # folder holding notify_hub.py
    },
    # Commands that move files or speak a binary protocol, which a mesh text
    # message cannot carry. Keyed by where the user is: "NODE", "BBS" or an
    # application's command name; matched on the first word typed. The
    # bridge answers `message` instead of passing them on.
    "telnet_only": {
        "message": "Use Telnet or RF to use this feature.",
        "commands": {"NODE": ["RMS"], "BBS": ["YAPP"],
                     "WX": ["GRIB", "YAPP", "FILE", "DL"]},
    },
    # Mesh-only rewrites of what BPQ sends (telnet/RF users are unaffected).
    #   node_prefix: replaces "ALIAS:CALL} " at the start of node replies;
    #                "" removes it, null leaves it alone.
    #   command_sep: joins the node's "?" command list, e.g. "|"; null = as is.
    #   welcome:     lines sent instead of the telnet CTEXT; [] = send CTEXT.
    #   join_after:  a blank line right after a line starting with one of
    #                these is dropped, e.g. ["Source:"] for the WX report.
    "display": {"node_prefix": None, "command_sep": None, "welcome": [],
                "compact_lists": False, "bbs_prompt": None,
                "menu_on_return": False, "app_names": None, "join_after": []},
    "users": {
        "N0CALL-Phone": {
            "pubkey": "",   # their public key (12+ hex chars) to allow DMs
            "callsign": "N0CALL",
            "telnet_user": "CHANGE_ME",
            "telnet_password": "CHANGE_ME",
        }
    },
}

# A first message that is only a greeting opens the session without being
# sent to the node (which would answer "Invalid command").
GREETINGS = {"", "HI", "HELLO", "HEY"}

log = logging.getLogger("meshbridge")


# ---------------------------------------------------------------- config/log

def setup_logging() -> None:
    (HERE / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        HERE / "logs" / "bpq_meshcore.log", maxBytes=1_000_000, backupCount=5,
        encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    log.addHandler(ch)


def load_config() -> Dict:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        log.error("Wrote a default %s - fill in the users block and restart.",
                  CONFIG_FILE.name)
        sys.exit(1)
    raw = CONFIG_FILE.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        lines = raw.splitlines()
        line = lines[e.lineno - 1] if 0 < e.lineno <= len(lines) else ""
        log.error("%s is not valid JSON: line %d col %d: %s\n    %s",
                  CONFIG_FILE.name, e.lineno, e.colno, e.msg, line)
        sys.exit(1)
    for k in ("radio", "channel", "bbs", "paging", "adverts", "display",
              "telnet_only", "guests", "web"):
        cfg[k] = {**DEFAULT_CONFIG[k], **cfg.get(k, {})}
    cfg.setdefault("users", {})
    return cfg


# ------------------------------------------------------------- text helpers

IAC = 255
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_telnet(data: bytes) -> bytes:
    """Remove telnet IAC negotiation (IAC cmd opt, IAC SB ... IAC SE)."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        cmd = data[i + 1]
        if cmd == IAC:                      # escaped 0xFF
            out.append(IAC)
            i += 2
        elif cmd == 250:                    # SB ... IAC SE
            end = data.find(bytes([IAC, 240]), i + 2)
            i = n if end < 0 else end + 2
        elif cmd in (251, 252, 253, 254):   # WILL WONT DO DONT + option
            i += 3
        else:
            i += 2
    return bytes(out)


def clean_text(s: str) -> str:
    """Telnet output -> plain lines: unify line ends, drop control chars,
    trim trailing spaces, collapse runs of blank lines."""
    s = _ANSI.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    s = s.replace("\t", "  ")
    out: List[str] = []
    for ln in (ln.rstrip() for ln in s.split("\n")):
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def trim_for_mesh(s: str) -> str:
    """Drop what costs airtime and says nothing on a phone: the FBB SID
    line ([BPQ-...$]) and ASCII-art / rule lines (mostly punctuation)."""
    keep = []
    for ln in s.split("\n"):
        t = ln.strip()
        if t.startswith("[") and t.endswith("$]"):
            continue
        if t and sum(ch.isalnum() for ch in t) < 0.4 * len(t.replace(" ", "")):
            continue
        keep.append(ln)
    return clean_text("\n".join(keep))


CONT = "(cont) "
MSG_MAX = 140   # bytes in one mesh text message (part marker and notes included)


def number_parts(chunks: List[str]) -> List[str]:
    """"... (1/3)", "... (2/3)" when a reply takes more than one message, so
    the reader knows more is on the way."""
    n = len(chunks)
    if n < 2:
        return chunks
    return [f"{c} ({i}/{n})" for i, c in enumerate(chunks, 1)]
_NODE_PREFIX = re.compile(r"^[A-Z0-9#]{1,6}:[A-Z0-9]{1,6}(?:-\d{1,2})?\} ")

# A BPQMail message-list line (L, LM, LL, LB ...):
# "5418   16-Nov PY    1298 N8FLA  @N4SFL  N4SFL  Welcome to the N4SFL BBS"
#  number date   type+status size to [@at] from  subject
_BBS_LIST_LINE = re.compile(
    r"^(?P<num>\d+)\s+\d{1,2}-[A-Za-z]{3}\s+(?P<type>[BPT])(?P<status>[A-Z$])\s+\d+"
    r"\s+(?P<to>\S+)\s+(?:@(?P<at>\S*)\s+)?(?P<frm>\S+)(?:\s+(?P<subj>.*))?$")


def compact_list_line(line: str) -> str:
    """'MSG#5418 from N4SFL RE: Welcome', with NEW on unread personal or
    traffic messages and [TO@AT] (the category) on bulletins. Lines that
    are not list lines come back unchanged."""
    m = _BBS_LIST_LINE.match(line.rstrip())
    if not m:
        return line
    out = f"MSG#{m['num']}"
    if m["type"] != "B" and m["status"] == "N":
        out += " NEW"
    out += f" from {m['frm']} RE: {(m['subj'] or '').strip()}"
    if m["type"] == "B":
        out += f" [{m['to']}{'@' + m['at'] if m['at'] else ''}]"
    return out


_BBS_PROMPT = re.compile(r"^de [A-Z0-9-]{3,9}>\s*$")
_RETURNED = re.compile(r"^Returned to Node \S+\s*$")
_CONNECTED_BBS = re.compile(r"Connected to BBS\b", re.I)
_READ_NUM = re.compile(r"^([RrKk])(\d+)$")
_CONNECTED_APP = re.compile(r"^\*\*\* Connected to (\S+)\s*$")


def mesh_format(text: str, display: Dict) -> str:
    """Mesh-only rewrites: the node's "ALIAS:CALL} " prefix, the "?"
    command list (a prefixed line of nothing but upper-case words), the
    BBS's "de CALL>" prompt (bbs_prompt), and, with compact_lists, BBS
    message-list lines. With menu_on_return, BPQ's "Returned to Node"
    line is dropped: the node's command list follows in its place."""
    prefix = display.get("node_prefix")
    sep = display.get("command_sep")
    compact = display.get("compact_lists")
    bbs_prompt = display.get("bbs_prompt")
    join = tuple(display.get("join_after") or ())
    out = []
    for line in text.split("\n"):
        if join and line == "" and out and out[-1].startswith(join):
            continue
        if display.get("menu_on_return") and _RETURNED.match(line):
            continue
        if bbs_prompt and _BBS_PROMPT.match(line):
            line = bbs_prompt
        names = display.get("app_names")
        if names is not None:
            m = _CONNECTED_APP.match(line)
            if m:   # "*** Connected to SPOTS" -> "*Connected to Live Spots*"
                line = f"*Connected to {names.get(m[1], m[1])}*"
        if compact:
            line = compact_list_line(line)
        m = _NODE_PREFIX.match(line)
        if m:
            rest = line[m.end():]
            words = rest.split()
            if sep and len(words) >= 5 and all(re.fullmatch(r"[A-Z0-9]+", w) for w in words):
                rest = sep.join(words)
            if prefix is None:
                line = m.group(0) + rest
            else:
                line = (prefix + " " if prefix else "") + rest
        out.append(line)
    return "\n".join(out)


def chunk_text(s: str, limit: int) -> List[str]:
    """Split into chunks of at most `limit` chars, breaking at line ends
    first, then after a space or "|", then hard. Lines are packed together
    while they fit, joined by newlines (the MeshCore apps show them as line
    breaks). A line too long for one message carries on in the next, which
    starts with CONT so it reads as a continuation."""
    chunks: List[str] = []
    cur = ""
    for line in s.split("\n"):
        first = True
        while len(line) > (limit if first else limit - len(CONT)):
            room = limit if first else limit - len(CONT)
            cut = max(line.rfind(" ", 0, room + 1), line.rfind("|", 0, room) + 1)
            if cut < room // 2:
                cut = room
            piece, line = line[:cut].rstrip(), line[cut:].lstrip()
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(piece if first else CONT + piece)
            first = False
        if not first and line:
            chunks.append(CONT + line)
            continue
        cand = line if not cur else cur + "\n" + line
        if len(cand) <= limit:
            cur = cand
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


# ------------------------------------------------------------ node session

class NodeSession:
    """One telnet connection into BPQ on behalf of one mesh user. After
    login a pump task forwards whatever BPQ sends, each time it goes quiet."""

    def __init__(self, cfg: Dict, user: Dict, label: str, on_output):
        self.cfg = cfg["bbs"]
        self.display = cfg.get("display", {})
        self.user = user
        self.label = label
        self.on_output = on_output          # async fn(text, closed: bool)
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.closed = True
        self.last_used = time.monotonic()
        self.pump_task: Optional[asyncio.Task] = None
        self.in_bbs = False
        self.in_app = False   # inside the BBS or an application, not at the node
        self.app = "NODE"     # where the user is: NODE, BBS or the app's command
        self.held = ""      # text kept back to go out with the node's "?" list

    async def _read_until(self, needle: bytes, timeout: float) -> bytes:
        buf = b""
        end = time.monotonic() + timeout
        while needle.lower() not in buf.lower():
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no {needle.decode()} prompt from BPQ")
            data = await asyncio.wait_for(self.reader.read(4096), left)
            if not data:
                raise ConnectionError("BPQ closed the connection during login")
            buf += strip_telnet(data)
        return buf

    async def open(self) -> None:
        t = self.cfg["login_timeout_s"]
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.cfg["host"], self.cfg["port"]), t)
        self.closed = False
        await self._read_until(b"Username:", t)
        self.writer.write(self.user["telnet_user"].encode() + b"\r\n")
        await self._read_until(b"Password:", t)
        self.writer.write(self.user["telnet_password"].encode() + b"\r\n")
        await self.writer.drain()
        # The telnet CTEXT arrives right after login. Read it here, before the
        # pump starts, so the configured mesh welcome can stand in for it.
        ctext = b""
        end = time.monotonic() + t
        while time.monotonic() < end:
            try:
                data = await asyncio.wait_for(self.reader.read(4096), self.cfg["quiet_s"])
            except asyncio.TimeoutError:
                if ctext:
                    break
                continue
            if not data:
                break
            ctext += strip_telnet(data)
        welcome = self.display.get("welcome") or []
        if welcome and b"invalid" not in ctext.lower():
            await self.on_output("\n".join(welcome), False)
        else:
            await self._emit(ctext, False)
        self.pump_task = asyncio.create_task(self._pump())
        log.info("[%s] node session open as %s", self.label, self.user["callsign"])

    async def _pump(self) -> None:
        quiet = self.cfg["quiet_s"]
        buf = b""
        try:
            while True:
                try:
                    data = await asyncio.wait_for(self.reader.read(4096), quiet)
                except asyncio.TimeoutError:
                    if buf:
                        text, buf = buf, b""
                        await self._emit(text, False)
                    continue
                if not data:
                    break
                buf += strip_telnet(data)
        except (ConnectionError, OSError) as e:
            log.warning("[%s] telnet read ended: %s", self.label, e)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] telnet pump failed", self.label)
        self.closed = True
        log.info("[%s] node session closed by BPQ", self.label)
        await self._emit(buf, True)

    async def _emit(self, raw: bytes, closed: bool) -> None:
        plain = trim_for_mesh(clean_text(raw.decode("latin-1")))
        if _CONNECTED_BBS.search(plain):
            self.in_bbs = True
        lines = plain.split("\n")
        for l in lines:
            m = _CONNECTED_APP.match(l)
            if m:
                self.in_app, self.app = True, m[1].upper()
        if self.in_bbs:
            self.in_app, self.app = True, "BBS"
        returned = any(_RETURNED.match(l) for l in lines)
        if returned:
            self.in_bbs = self.in_app = False
            self.app = "NODE"
        text = mesh_format(plain, self.display)
        if "invalid" in text.lower() and "password" in text.lower():
            log.error("[%s] BPQ rejected the telnet login", self.label)
        if self.held:
            text, self.held = (self.held + "\n" + text) if text else self.held, ""
        if returned and not closed and self.display.get("menu_on_return"):
            # Back at the node: ask it for its command list and send what we
            # have along with that reply, so it all goes out together.
            self.held = text
            try:
                await self.send_line("?")
                return
            except Exception:
                log.exception("[%s] asking the node for its command list failed", self.label)
                text, self.held = self.held, ""
        try:
            await self.on_output(text, closed)
        except Exception:
            log.exception("[%s] delivering output failed", self.label)

    async def send_line(self, line: str) -> None:
        self.last_used = time.monotonic()
        self.writer.write(line.encode("latin-1", "replace") + b"\r")
        await self.writer.drain()

    async def close(self) -> None:
        self.closed = True
        if self.pump_task and self.pump_task is not asyncio.current_task():
            self.pump_task.cancel()
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                log.exception("[%s] error closing telnet", self.label)
        self.writer = None


class AppSession(NodeSession):
    """A guest's or member's connection straight to one node application's
    TCP port, the way BPQ itself connects: the first line is the callsign,
    then the user's lines both ways. No BPQ login, no RF."""

    def __init__(self, cfg: Dict, callsign: str, app: str, port: int,
                 label: str, on_output):
        super().__init__(cfg, {"callsign": callsign}, label, on_output)
        self.host = cfg["guests"]["host"]
        self.port = port
        self.in_app, self.app = True, app

    async def open(self) -> None:
        t = self.cfg["login_timeout_s"]
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), t)
        self.closed = False
        self.writer.write(self.user["callsign"].encode() + b"\r\n")
        await self.writer.drain()
        self.pump_task = asyncio.create_task(self._pump())
        log.info("[%s] app %s open as %s", self.label, self.app, self.user["callsign"])

    async def _emit(self, raw: bytes, closed: bool) -> None:
        plain = trim_for_mesh(clean_text(raw.decode("latin-1")))
        try:
            await self.on_output(mesh_format(plain, self.display), closed)
        except Exception:
            log.exception("[%s] delivering output failed", self.label)

    async def send_line(self, line: str) -> None:
        self.last_used = time.monotonic()
        self.writer.write(line.encode("latin-1", "replace") + b"\r\n")
        await self.writer.drain()


# ------------------------------------------------------------ members

MEMBERS_FILE = HERE / "mesh_members.json"
_CALLSIGN = re.compile(r"^(?=.*\d)[A-Z0-9]{3,7}$")


def load_members() -> Dict[str, Dict]:
    """Radio key prefix -> {callsign, name, status: pending|approved|denied, ...}."""
    if not MEMBERS_FILE.exists():
        return {}
    try:
        return json.loads(MEMBERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("%s unreadable; starting with no members", MEMBERS_FILE.name)
        return {}


def save_members(members: Dict[str, Dict]) -> None:
    tmp = MEMBERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(members, indent=2) + "\n", encoding="utf-8")
    tmp.replace(MEMBERS_FILE)


def verify_callsign(url_tmpl: str, call: str) -> Dict:
    """Ask callook.info (US licenses) about `call`. Returns {"status":
    VALID|INVALID|UNKNOWN, "name", "oper_class"}; UNKNOWN when the lookup
    could not be made, which the sysop then decides by hand."""
    try:
        req = urllib.request.Request(url_tmpl.format(call=call),
                                     headers={"User-Agent": "bpq-meshcore-bridge"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("callsign lookup for %s failed: %s", call, e)
        return {"status": "UNKNOWN", "name": "", "oper_class": ""}
    return {"status": str(d.get("status", "UNKNOWN")).upper(),
            "name": str(d.get("name") or "").title(),
            "oper_class": str((d.get("current") or {}).get("operClass") or "").title()}


# ---------------------------------------------------------------- bridge

class UserState:
    def __init__(self) -> None:
        self.session: Optional[NodeSession] = None
        self.pending: List[str] = []
        self.queued = 0                   # messages queued since the last command
        self.cut = False                  # output capped; drop the rest
        self.dest: Optional[str] = None   # None = channel, else DM pubkey prefix
        self.lock = asyncio.Lock()        # one mesh message at a time per user
        self.close_note: Optional[str] = None  # sent when the session ends


async def send_lines(session: "NodeSession", body: str) -> None:
    """One mesh message can hold several lines (a BBS message body ending
    in /EX). Each goes as its own line, with a pause between them: BPQ
    telnet reads only the first line of a packet that holds "text CR /ex
    CR", so the BBS never saw /EX and kept taking commands as text."""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if i:
            await asyncio.sleep(0.5)
        await session.send_line(line.rstrip())


def key_prefix(k: str) -> str:
    return re.sub(r"[^0-9a-f]", "", (k or "").lower())[:12]


class Bridge:
    def __init__(self, cfg: Dict, web: Optional[RadioWeb] = None):
        self.cfg = cfg
        self.web = web
        self.mc: Optional[MeshCore] = None
        self.users: Dict[str, UserState] = {}
        self.radio_lock = asyncio.Lock()   # one radio: sends go out in order
        self.chan = cfg["channel"].get("index")
        self.members = load_members()
        self.seen_dm = {}                  # (key, sender time, text) -> when seen
        self.greeted = set()               # guests already given the welcome
        self.by_key = {}
        for name, u in cfg["users"].items():
            kp = key_prefix(u.get("pubkey", ""))
            if len(kp) == 12:
                self.by_key[kp] = name

    # -- radio out
    async def say(self, text: str, dest: Optional[str] = None) -> None:
        """Send one mesh message: to the channel, or as a DM to `dest`."""
        async with self.radio_lock:
            if dest is None:
                if self.chan is None:
                    log.error("no channel configured; dropped %r", text[:40])
                    return
                r = await self.mc.commands.send_chan_msg(self.chan, text)
                if r.type == EventType.ERROR:
                    log.error("send_chan_msg failed: %s", r.payload)
                if self.web:
                    self.web.record("out", "chan", text, channel=self.chan,
                                    name="(bridge)", via="bridge")
            else:
                p = self.cfg["paging"]
                r = await self.mc.commands.send_msg_with_retry(
                    dest, text, max_attempts=p["dm_attempts"], flood_after=1,
                    timeout=p["ack_wait_s"])
                acked = not (r is None or r.type == EventType.ERROR)
                if not acked:
                    log.error("DM to %s not acknowledged: %r", dest, text[:40])
                if self.web:
                    self.web.record("out", "dm", text, peer=dest,
                                    name=self.web.contact_name(dest), via="bridge",
                                    acked=acked)
            await asyncio.sleep(self.cfg["paging"]["gap_s"])

    async def page_out(self, st: UserState) -> None:
        p = self.cfg["paging"]
        n = p["chunks_per_reply"]
        batch, st.pending = st.pending[:n], st.pending[n:]
        batch = number_parts(batch)       # ... (1/3) counts this page only
        for i, c in enumerate(batch):
            if i == len(batch) - 1 and st.pending:
                pages = -(-len(st.pending) // n)  # one page = one M reply
                tag = f"\n[M=More, {pages} page{'s' if pages != 1 else ''} left]"
                if len((c + tag).encode()) > MSG_MAX:
                    await self.say(c, st.dest)
                    c = tag.strip()
                else:
                    c += tag
            await self.say(c, st.dest)

    def cap(self, name: str, st: UserState, new: List[str]) -> List[str]:
        """Keep one command's output within paging.max_pages pages."""
        p = self.cfg["paging"]
        limit = (p.get("max_pages") or 0) * p["chunks_per_reply"]
        if not limit:
            return new
        if st.cut:
            log.info("[%s] dropped %d more messages (over the cap)", name, len(new))
            return []
        room = limit - st.queued
        if len(new) <= room:
            st.queued += len(new)
            return new
        log.info("[%s] output cut at %d messages, dropped %d", name, limit,
                 len(new) - max(room, 0))
        note = f"[List cut at {limit} msgs. Try LL 10 or L 5590-5595]"
        keep = new[:max(room, 1)]
        last = keep[-1] + "\n" + note
        keep[-1] = last if len(last) <= p["chunk_chars"] else note
        st.cut = True
        st.queued = limit
        return keep

    def output_handler(self, name: str, st: UserState):
        async def handler(text: str, closed: bool) -> None:
            note = st.close_note or "[Disconnected. Send anything to reconnect.]"
            if closed:
                text = (text + "\n" if text else "") + note
                st.session = None
            if not text:
                return
            log.info("[%s] -> %d chars (%s)", name, len(text), st.dest or "channel")
            new = self.cap(name, st, chunk_text(text, self.cfg["paging"]["chunk_chars"]))
            if closed and st.cut and not new:
                new = chunk_text(note, self.cfg["paging"]["chunk_chars"])
            if not new:
                return
            # Output that arrives while earlier pages wait is queued behind them.
            if st.pending:
                st.pending.extend(new)
                return
            st.pending = new
            await self.page_out(st)
        return handler

    # -- radio in
    async def on_channel(self, ev) -> None:
        p = ev.payload or {}
        if self.chan is None or p.get("channel_idx") != self.chan:
            return
        raw = str(p.get("text", ""))
        name, sep, body = raw.partition(": ")
        if not sep:
            log.info("ignored channel text with no sender: %r", raw[:60])
            return
        if name not in self.cfg["users"]:
            log.warning("ignored channel %r: sender %r not in users", body[:40], name)
            return
        await self.dispatch(name, None, body)

    async def on_dm(self, ev) -> None:
        p = ev.payload or {}
        # Only plain text. CLI replies from a repeater and room-server posts
        # (txt_type 1 and 2) are not someone talking to the bridge.
        if p.get("txt_type", 0) != 0:
            return
        kp = key_prefix(p.get("pubkey_prefix", ""))
        body = str(p.get("text", ""))
        if self.web and self.web.talking(kp):
            # The sysop is chatting with this contact on the radio page.
            log.info("DM %r from %s is for the radio page (talk mode)", body[:40], kp)
            return
        # A sender whose ACK got lost retries the same message; answer it once.
        now = time.time()
        self.seen_dm = {k: t for k, t in self.seen_dm.items() if now - t < 300}
        dup = (kp, p.get("sender_timestamp"), body)
        if dup in self.seen_dm:
            log.info("duplicate DM %r from %s ignored (sender retry)", body[:40], kp)
            return
        self.seen_dm[dup] = now
        name = self.by_key.get(kp)
        if not name:
            if self.cfg["guests"].get("enabled") and len(kp) == 12:
                await self.dispatch_guest(kp, body)
                return
            log.warning("ignored DM %r from unlisted key %s - add it as \"pubkey\" "
                        "to a user to allow it", body[:40], kp)
            return
        await self.dispatch(name, kp, body)

    async def dispatch(self, name: str, dest: Optional[str], body: str) -> None:
        log.info("[%s] <- %r via %s", name, body, dest or "channel")
        st = self.users.setdefault(name, UserState())
        try:
            async with st.lock:
                st.dest = dest              # reply the way they came in
                await self.handle(name, self.cfg["users"][name], body.strip())
        except Exception as e:
            log.exception("[%s] handling %r failed", name, body)
            await self.say(f"Bridge error: {type(e).__name__}: {str(e)[:70]}", dest)

    async def handle(self, name: str, user: Dict, body: str) -> None:
        st = self.users[name]
        if await self.admin_command(name, body, st.dest):
            return
        if body.upper() == "M" and st.pending:
            await self.page_out(st)
            return
        st.pending = []
        st.queued, st.cut = 0, False      # a new command gets a fresh cap

        fresh = False
        if not st.session or st.session.closed:
            st.session = NodeSession(self.cfg, user, name, self.output_handler(name, st))
            try:
                await st.session.open()
            except Exception as e:
                log.exception("[%s] node login failed", name)
                await st.session.close()
                st.session = None
                await self.say(f"Node unreachable: {str(e)[:80]}", st.dest)
                return
            fresh = True
        if fresh and body.upper() in GREETINGS:
            return                      # the node's welcome text is the reply
        word = (body.split() or [""])[0].upper()
        blocked = self.cfg["telnet_only"]["commands"].get(st.session.app, [])
        if word in (w.upper() for w in blocked):
            log.info("[%s] %r is telnet-only (in %s)", name, body, st.session.app)
            await self.say(self.cfg["telnet_only"]["message"], st.dest)
            return
        if body.upper() == "NODE" and not st.session.in_app:
            # Already at the node (the app ended, or the session timed out).
            # BPQ would read NODE as NODES and list every node heard.
            if fresh:
                return                  # the welcome text is the reply
            body = "?"
        if fresh:
            await asyncio.sleep(0.5)    # let the welcome text arrive first
        m = _READ_NUM.match(body.strip())
        if m and st.session.in_bbs:
            body = f"{m[1]} {m[2]}"     # the BBS wants "R 5387", phones type R5387
        bare = self.cfg["bbs"].get("bare_list")
        if bare and st.session.in_bbs and body.strip().upper() == "L":
            body = bare                 # a bare L would list every message
        await send_lines(st.session, body)

    # -- guests and members
    async def reply(self, text: str, dest: Optional[str]) -> None:
        """say(), split to the mesh message size like node output is."""
        # Not paged (no M=More note), so a message can be nearly full: room is
        # left only for the " (1/2)" marker.
        for c in number_parts(chunk_text(text, MSG_MAX - 8)):
            await self.say(c, dest)

    def member(self, kp: str) -> Optional[Dict]:
        m = self.members.get(kp)
        return m if m and m.get("status") == "approved" else None

    def guest_apps(self, kp: str) -> Dict[str, int]:
        g = self.cfg["guests"]
        apps = {k.upper(): int(v) for k, v in g["apps"].items()}
        if self.member(kp):
            apps.update({k.upper(): int(v) for k, v in g["member_apps"].items()})
        return apps

    def guest_menu(self, kp: str) -> str:
        g = self.cfg["guests"]
        # The same look as the node's "?" list for users: "N4SFL: WX | SPOTS".
        d = self.cfg["display"]
        sep = d.get("command_sep") or " | "
        prefix = d.get("node_prefix")
        apps = (prefix + " " if prefix else "") + sep.join([*self.guest_apps(kp), "BBS"])
        m = self.members.get(kp) or {}
        if m.get("status") == "approved":
            head = f"{g['title']} - {m['callsign']}"
            tail = ""
        elif m.get("status") == "pending":
            head = f"{g['title']} - guest"
            tail = f"\n{m['callsign']} is waiting for sysop approval."
        else:
            head = ""                     # a plain guest: the list says it all
            tail = "\nSend MYCALL <callsign> for more."
        return (f"{head}\n" if head else "") + f"{apps}{tail}\n[?=Commands, BYE=Log off]"

    async def dispatch_guest(self, kp: str, body: str) -> None:
        m = self.member(kp)
        name = m["callsign"] if m else f"GUEST{kp[:6].upper()}"
        log.info("[%s] <- %r via %s (guest)", name, body, kp)
        st = self.users.setdefault("dm:" + kp, UserState())
        try:
            async with st.lock:
                st.dest = kp
                await self.handle_guest(name, kp, st, body.strip())
        except Exception as e:
            log.exception("[%s] handling %r failed", name, body)
            await self.reply(f"Bridge error: {type(e).__name__}: {str(e)[:70]}", kp)

    async def handle_guest(self, name: str, kp: str, st: UserState, body: str) -> None:
        if kp not in self.greeted:
            # First text from this radio, whatever it said: welcome and menu.
            self.greeted.add(kp)
            await self.reply(f"Welcome to {self.cfg['guests']['title']}!\n"
                             + self.guest_menu(kp), kp)
            return
        if body.upper() == "M" and st.pending:
            await self.page_out(st)
            return
        st.pending = []
        st.queued, st.cut = 0, False
        word = (body.split() or [""])[0].upper()

        if st.session and not st.session.closed:        # inside an app
            blocked = self.cfg["telnet_only"]["commands"].get(st.session.app, [])
            if word in (w.upper() for w in blocked):
                await self.reply(self.cfg["telnet_only"]["message"], kp)
                return
            await send_lines(st.session, body)
            return

        apps = self.guest_apps(kp)
        if word in GREETINGS or word in ("?", "HELP", "MENU", "NODE"):
            await self.reply(self.guest_menu(kp), kp)
        elif word in ("BYE", "Q", "QUIT", "EXIT"):
            await self.reply("73! Send anything to come back.", kp)
        elif word in ("MYCALL", "CALL"):
            await self.signup(name, kp, body.split()[1:])
        elif word in apps:
            st.close_note = self.guest_menu(kp)
            st.session = AppSession(self.cfg, name, word, apps[word], name,
                                    self.output_handler(name, st))
            try:
                await st.session.open()
            except Exception as e:
                log.warning("[%s] app %s unreachable: %s", name, word, e)
                st.session = None
                await self.reply(f"{word} is offline right now. Try again later.", kp)
        elif word == "BBS":
            await self.reply(self.cfg["guests"]["bbs_info"], kp)
        elif word in ("CHAT", "CONNECT", "C", "RMS"):
            await self.reply(f"{word} is for licensed hams with a node login. "
                           "Send MYCALL <callsign> to ask.", kp)
        else:
            await self.reply(f"Unknown command {word[:12]}.\n" + self.guest_menu(kp), kp)

    async def signup(self, name: str, kp: str, args: List[str]) -> None:
        call = (args[0] if args else "").upper()
        if not _CALLSIGN.match(call):
            await self.reply("Send MYCALL and your callsign, e.g. MYCALL N0XYZ.", kp)
            return
        m = self.members.get(kp) or {}
        if m.get("status") == "approved":
            await self.reply(f"You are already approved as {m['callsign']}.", kp)
            return
        v = await asyncio.to_thread(verify_callsign, self.cfg["guests"]["verify_url"], call)
        if v["status"] == "INVALID":
            await self.reply(f"{call} is not in the FCC database (US licenses only "
                           "for now). Check it and send CALL again.", kp)
            return
        self.members[kp] = {"callsign": call, "name": v["name"],
                            "oper_class": v["oper_class"], "verified": v["status"] == "VALID",
                            "status": "pending",
                            "requested": datetime.now().isoformat(timespec="seconds")}
        save_members(self.members)
        first = v["name"].split()[0] if v["name"] else call
        await self.reply(f"Thanks {first}. {call} is waiting for sysop approval; "
                       "you'll get a message here.", kp)
        who = f"{call} ({v['name'] or 'name unknown'}"
        who += f", {v['oper_class']})" if v["oper_class"] else ")"
        if v["status"] != "VALID":
            who += " - NOT verified, lookup failed"
        log.info("[%s] signup request: %s, radio %s", name, who, kp)
        await self.tell_admins(f"Signup: {who}. Reply APPROVE {call} or DENY {call}.")
        await asyncio.to_thread(self.notify, f"Mesh signup: {call}",
                                f"{who} asked for N4SFL mesh access from radio {kp}.\n"
                                f"Approve from your radio: APPROVE {call}")

    async def tell_admins(self, text: str) -> None:
        for u in self.cfg["users"].values():
            kp = key_prefix(u.get("pubkey", ""))
            if len(kp) == 12:
                await self.reply(text, kp)

    def notify(self, subject: str, body: str) -> None:
        g = self.cfg["guests"]
        if not g.get("notify_project") or not g.get("notify_path"):
            return
        try:
            if g["notify_path"] not in sys.path:
                sys.path.insert(0, g["notify_path"])
            from notify_hub import send
            r = send(g["notify_project"], "alert", subject, body)
            if not r.ok:
                log.error("signup alert not delivered: %s", r.summary())
        except Exception:
            log.exception("signup alert failed")

    async def admin_command(self, name: str, body: str, dest: Optional[str]) -> bool:
        """APPROVE <call>, DENY <call>, PENDING - from sysop radios only."""
        words = body.split()
        cmd = words[0].upper() if words else ""
        if cmd not in ("APPROVE", "DENY", "PENDING"):
            return False
        if cmd == "PENDING":
            p = [f"{m['callsign']} ({m.get('name') or '?'})"
                 for m in self.members.values() if m.get("status") == "pending"]
            await self.reply("Pending: " + (", ".join(p) if p else "none"), dest)
            return True
        call = words[1].upper() if len(words) > 1 else ""
        if not self.decide(cmd, call, name):
            await self.reply(f"No signup for {call or '?'}. Send PENDING for the list.", dest)
            return True
        await self.reply(f"{call} {'approved' if cmd == 'APPROVE' else 'denied'}.", dest)
        await self.tell_decision(cmd, call)
        return True

    def decide(self, cmd: str, call: str, by: str) -> bool:
        """Mark every signup for `call` approved or denied. False if none."""
        hits = [m for m in self.members.values() if m.get("callsign") == call]
        for m in hits:
            m["status"] = "approved" if cmd == "APPROVE" else "denied"
            m["decided"] = datetime.now().isoformat(timespec="seconds")
            m["decided_by"] = by
        if hits:
            save_members(self.members)
            log.info("[%s] %s %s", by, cmd.lower(), call)
        return bool(hits)

    async def tell_decision(self, cmd: str, call: str) -> None:
        """DM the radio(s) behind `call` the sysop's answer."""
        for kp, m in list(self.members.items()):
            if m.get("callsign") != call:
                continue
            if cmd == "APPROVE":
                await self.reply(f"Welcome {call}! You are approved. Send ? for your menu.", kp)
            else:
                await self.reply("Sorry, your request was not approved. Guest access "
                               "still works: send ?", kp)

    # -- housekeeping
    async def idle_reaper(self) -> None:
        limit = self.cfg["bbs"]["idle_close_s"]
        while True:
            await asyncio.sleep(30)
            for name, st in list(self.users.items()):
                s = st.session
                if s and not s.closed and time.monotonic() - s.last_used > limit:
                    log.info("[%s] idle %ds, closing", name, limit)
                    st.session = None
                    st.pending = []
                    await s.close()
                    await self.say(f"{name}: disconnected after {limit // 60} min idle.", st.dest)

    async def advertiser(self) -> None:
        hours = self.cfg["adverts"].get("flood_every_h") or 0
        while True:
            async with self.radio_lock:
                r = await self.mc.commands.send_advert(flood=True)
                if r.type == EventType.ERROR:
                    log.error("advert failed: %s", r.payload)
                else:
                    log.info("flood advert sent")
            if hours <= 0:
                return
            await asyncio.sleep(hours * 3600)

    async def run(self) -> None:
        r = self.cfg["radio"]
        self.mc = await MeshCore.create_serial(r["port"], r["baud"])
        if self.mc is None:
            raise ConnectionError(f"no MeshCore companion on {r['port']}")
        try:
            await self._serve(r)
        finally:
            # Always let go of the port, or every retry gets "Access is denied".
            for st in self.users.values():
                if st.session:
                    await st.session.close()
            try:
                await self.mc.disconnect()
            except Exception:
                log.exception("error disconnecting radio")

    async def _serve(self, r: Dict) -> None:
        self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self.on_channel)
        self.mc.subscribe(EventType.CONTACT_MSG_RECV, self.on_dm)
        if self.web:
            self.web.attach(self)
            try:
                dq = await self.mc.commands.send_device_query()
                if dq.type != EventType.ERROR:
                    self.web.device = dq.payload
            except Exception:
                log.exception("device query for the web page failed")
        await self.mc.start_auto_message_fetching()
        if self.cfg["guests"].get("enabled"):
            # The radio can only answer a DM from a contact. With manual add
            # on, a stranger's advert is not stored and their DMs go unanswered.
            info = self.mc.self_info or {}
            log.info("radio manual_add_contacts = %s", info.get("manual_add_contacts", "not reported"))
            if info.get("manual_add_contacts"):
                res = await self.mc.commands.set_manual_add_contacts(False)
                if res.type == EventType.ERROR:
                    log.error("could not turn on auto-add contacts: %s", res.payload)
                else:
                    log.info("guests: turned on auto-add contacts on the radio")
        log.info("Listening on %s: channel %s, DMs from %d listed key(s); users: %s; "
                 "guests %s, %d member(s)",
                 r["port"], self.chan, len(self.by_key),
                 ", ".join(self.cfg["users"]) or "(nobody)",
                 "on" if self.cfg["guests"].get("enabled") else "off",
                 sum(1 for m in self.members.values() if m.get("status") == "approved"))
        tasks = [asyncio.create_task(self.idle_reaper()),
                 asyncio.create_task(self.advertiser())]
        try:
            while self.mc.is_connected:
                await asyncio.sleep(5)
            log.error("radio disconnected")
        finally:
            for t in tasks:
                t.cancel()


async def main() -> None:
    cfg = load_config()
    for name, u in cfg["users"].items():
        if "CHANGE_ME" in (u.get("telnet_user"), u.get("telnet_password")):
            log.error("users[%s] still has CHANGE_ME credentials", name)
            sys.exit(1)
    web = None
    if cfg["web"].get("enabled"):
        web = RadioWeb(int(cfg["web"]["port"]))
        try:
            await web.start()
        except OSError as e:
            log.error("radio web page not started (port %s): %s", cfg["web"]["port"], e)
            web = None
    while True:
        try:
            await Bridge(cfg, web).run()
        except Exception:
            log.exception("bridge stopped")
        log.info("restarting in 30 s")
        await asyncio.sleep(30)


if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped by Ctrl+C")
