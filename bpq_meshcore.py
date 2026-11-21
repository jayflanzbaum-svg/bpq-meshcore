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

Phase 1: one private channel and an allow-list of sender names. Channel
sender names are NOT authenticated (anyone holding the channel key can type
any name), so the channel key is the real access control. Phase 2 moves
sessions to direct messages keyed by public key (see README).
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
    },
    "paging": {"chunk_chars": 118, "chunks_per_reply": 4, "gap_s": 2.5},
    "users": {
        "N0CALL-Phone": {
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
    for k in ("radio", "channel", "bbs", "paging"):
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


def chunk_text(s: str, limit: int) -> List[str]:
    """Split into chunks of at most `limit` chars, breaking at line ends
    first, then at spaces, then hard. Lines are packed together while they
    fit, joined by newlines (the MeshCore apps show them as line breaks)."""
    chunks: List[str] = []
    cur = ""
    for line in s.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit + 1)
            if cut < limit // 2:
                cut = limit
            piece, line = line[:cut].rstrip(), line[cut:].lstrip()
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(piece)
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
        self.user = user
        self.label = label
        self.on_output = on_output          # async fn(text, closed: bool)
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.closed = True
        self.last_used = time.monotonic()
        self.pump_task: Optional[asyncio.Task] = None

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
        text = trim_for_mesh(clean_text(raw.decode("latin-1")))
        if "invalid" in text.lower() and "password" in text.lower():
            log.error("[%s] BPQ rejected the telnet login", self.label)
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
        self.lock = asyncio.Lock()   # one mesh message at a time per user


class Bridge:
    def __init__(self, cfg: Dict):
        self.cfg = cfg
        self.mc: Optional[MeshCore] = None
        self.users: Dict[str, UserState] = {}
        self.radio_lock = asyncio.Lock()   # one radio: sends go out in order
        self.chan = cfg["channel"]["index"]

    # -- radio out
    async def say(self, text: str) -> None:
        async with self.radio_lock:
            r = await self.mc.commands.send_chan_msg(self.chan, text)
            if r.type == EventType.ERROR:
                log.error("send_chan_msg failed: %s", r.payload)
            await asyncio.sleep(self.cfg["paging"]["gap_s"])

    async def page_out(self, st: UserState) -> None:
        p = self.cfg["paging"]
        n = p["chunks_per_reply"]
        batch, st.pending = st.pending[:n], st.pending[n:]
        for i, c in enumerate(batch):
            if i == len(batch) - 1 and st.pending:
                tag = f"\n[M=more, {len(st.pending)} left]"
                if len(c) + len(tag) > p["chunk_chars"]:
                    await self.say(c)
                    c = tag.strip()
                else:
                    c += tag
            await self.say(c)

    def output_handler(self, name: str, st: UserState):
        async def handler(text: str, closed: bool) -> None:
            if closed:
                text = (text + "\n" if text else "") + "[Disconnected. Send anything to reconnect.]"
                st.session = None
            if not text:
                return
            log.info("[%s] -> %d chars", name, len(text))
            new = chunk_text(text, self.cfg["paging"]["chunk_chars"])
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
        if p.get("channel_idx") != self.chan:
            return
        raw = str(p.get("text", ""))
        name, sep, body = raw.partition(": ")
        if not sep:
            log.info("ignored channel text with no sender: %r", raw[:60])
            return
        user = self.cfg["users"].get(name)
        if not user:
            log.warning("ignored %r: sender %r not in users", body[:40], name)
            return
        log.info("[%s] <- %r", name, body)
        st = self.users.setdefault(name, UserState())
        try:
            async with st.lock:
                await self.handle(name, user, body.strip())
        except Exception as e:
            log.exception("[%s] handling %r failed", name, body)
            await self.say(f"Bridge error: {type(e).__name__}: {str(e)[:70]}")

    async def handle(self, name: str, user: Dict, body: str) -> None:
        st = self.users[name]
        if body.upper() == "M" and st.pending:
            await self.page_out(st)
            return
        st.pending = []

        fresh = False
        if not st.session or st.session.closed:
            st.session = NodeSession(self.cfg, user, name, self.output_handler(name, st))
            try:
                await st.session.open()
            except Exception as e:
                log.exception("[%s] node login failed", name)
                await st.session.close()
                st.session = None
                await self.say(f"Node unreachable: {str(e)[:80]}")
                return
            fresh = True
        if fresh and body.upper() in GREETINGS:
            return                      # the node's welcome text is the reply
        if fresh:
            await asyncio.sleep(0.5)    # let the welcome text arrive first
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
                    await self.say(f"{name}: disconnected after {limit // 60} min idle.")

    async def run(self) -> None:
        r = self.cfg["radio"]
        self.mc = await MeshCore.create_serial(r["port"], r["baud"])
        if self.mc is None:
            raise ConnectionError(f"no MeshCore companion on {r['port']}")
        self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self.on_channel)
        await self.mc.start_auto_message_fetching()
        log.info("Listening on %s channel %d for %s", r["port"], self.chan,
                 ", ".join(self.cfg["users"]) or "(nobody)")
        reaper = asyncio.create_task(self.idle_reaper())
        try:
            while self.mc.is_connected:
                await asyncio.sleep(5)
            log.error("radio disconnected")
        finally:
            reaper.cancel()
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
