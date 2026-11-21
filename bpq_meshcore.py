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
from pathlib import Path
from typing import Dict, List, Optional

from meshcore import EventType, MeshCore

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
    "paging": {"chunk_chars": 118, "chunks_per_reply": 4, "gap_s": 2.5,
               "max_pages": 5},
    "adverts": {"flood_every_h": 12},
    # Mesh-only rewrites of what BPQ sends (telnet/RF users are unaffected).
    #   node_prefix: replaces "ALIAS:CALL} " at the start of node replies;
    #                "" removes it, null leaves it alone.
    #   command_sep: joins the node's "?" command list, e.g. "|"; null = as is.
    #   welcome:     lines sent instead of the telnet CTEXT; [] = send CTEXT.
    "display": {"node_prefix": None, "command_sep": None, "welcome": [],
                "compact_lists": False, "bbs_prompt": None,
                "menu_on_return": False, "app_names": None},
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
    for k in ("radio", "channel", "bbs", "paging", "adverts", "display"):
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
    out = []
    for line in text.split("\n"):
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
        if self.in_bbs or any(_CONNECTED_APP.match(l) for l in lines):
            self.in_app = True
        returned = any(_RETURNED.match(l) for l in lines)
        if returned:
            self.in_bbs = self.in_app = False
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


# ---------------------------------------------------------------- bridge

class UserState:
    def __init__(self) -> None:
        self.session: Optional[NodeSession] = None
        self.pending: List[str] = []
        self.queued = 0                   # messages queued since the last command
        self.cut = False                  # output capped; drop the rest
        self.dest: Optional[str] = None   # None = channel, else DM pubkey prefix
        self.lock = asyncio.Lock()        # one mesh message at a time per user


def key_prefix(k: str) -> str:
    return re.sub(r"[^0-9a-f]", "", (k or "").lower())[:12]


class Bridge:
    def __init__(self, cfg: Dict):
        self.cfg = cfg
        self.mc: Optional[MeshCore] = None
        self.users: Dict[str, UserState] = {}
        self.radio_lock = asyncio.Lock()   # one radio: sends go out in order
        self.chan = cfg["channel"].get("index")
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
            else:
                r = await self.mc.commands.send_msg_with_retry(dest, text)
                if r is None or r.type == EventType.ERROR:
                    log.error("DM to %s not acknowledged: %r", dest, text[:40])
            await asyncio.sleep(self.cfg["paging"]["gap_s"])

    async def page_out(self, st: UserState) -> None:
        p = self.cfg["paging"]
        n = p["chunks_per_reply"]
        batch, st.pending = st.pending[:n], st.pending[n:]
        for i, c in enumerate(batch):
            if i == len(batch) - 1 and st.pending:
                pages = -(-len(st.pending) // n)  # one page = one M reply
                tag = f"\n[M=More, {pages} page{'s' if pages != 1 else ''} left]"
                if len(c) + len(tag) > p["chunk_chars"]:
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
            if closed:
                text = (text + "\n" if text else "") + "[Disconnected. Send anything to reconnect.]"
                st.session = None
            if not text:
                return
            log.info("[%s] -> %d chars (%s)", name, len(text), st.dest or "channel")
            new = self.cap(name, st, chunk_text(text, self.cfg["paging"]["chunk_chars"]))
            if closed and st.cut and not new:
                new = ["[Disconnected. Send anything to reconnect.]"]
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
        kp = key_prefix(p.get("pubkey_prefix", ""))
        body = str(p.get("text", ""))
        name = self.by_key.get(kp)
        if not name:
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
        await st.session.send_line(body)

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
        self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self.on_channel)
        self.mc.subscribe(EventType.CONTACT_MSG_RECV, self.on_dm)
        await self.mc.start_auto_message_fetching()
        log.info("Listening on %s: channel %s, DMs from %d listed key(s); users: %s",
                 r["port"], self.chan, len(self.by_key),
                 ", ".join(self.cfg["users"]) or "(nobody)")
        tasks = [asyncio.create_task(self.idle_reaper()),
                 asyncio.create_task(self.advertiser())]
        try:
            while self.mc.is_connected:
                await asyncio.sleep(5)
            log.error("radio disconnected")
        finally:
            for t in tasks:
                t.cancel()
            for st in self.users.values():
                if st.session:
                    await st.session.close()
            try:
                await self.mc.disconnect()
            except Exception:
                log.exception("error disconnecting radio")


async def main() -> None:
    cfg = load_config()
    for name, u in cfg["users"].items():
        if "CHANGE_ME" in (u.get("telnet_user"), u.get("telnet_password")):
            log.error("users[%s] still has CHANGE_ME credentials", name)
            sys.exit(1)
    while True:
        try:
            await Bridge(cfg).run()
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
