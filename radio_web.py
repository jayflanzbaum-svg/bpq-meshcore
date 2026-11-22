"""
radio_web.py - a local web page for managing the bridge radio.

The bridge holds the radio's USB port, so the official MeshCore app cannot
open it while the bridge runs. This serves a page on 127.0.0.1 that does the
same jobs through the bridge's own connection: messages in and out, contacts,
channels, radio settings, adverts, repeater/room-server admin, and the
gateway's sessions and signups.

Only this computer can reach it (it binds 127.0.0.1 and checks the Host
header). Every POST must carry the X-Radio header, which a page on another
site cannot add without a CORS preflight this server never answers, so a web
page you visit cannot drive the radio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from meshcore import EventType

log = logging.getLogger("meshbridge")

HERE = Path(__file__).resolve().parent
PAGE = HERE / "radio_web.html"
MSG_FILE = HERE / "logs" / "messages.jsonl"
KEEP = 2000          # messages kept in memory and shown
ADV_TYPES = {1: "Companion", 2: "Repeater", 3: "Room server", 4: "Sensor"}


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _ok(r) -> Any:
    """Event -> payload, raising on an ERROR event or no answer."""
    if r is None:
        raise ApiError(504, "the radio did not answer")
    if r.type == EventType.ERROR:
        raise ApiError(502, f"radio error: {r.payload}")
    return r.payload


def _jsonable(v):
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


class RadioWeb:
    def __init__(self, port: int):
        self.port = port
        self.bridge = None                       # set by the bridge on each (re)connect
        self.msgs: deque = deque(maxlen=KEEP)
        self.seq = 0
        self.server: Optional[asyncio.base_events.Server] = None
        self.device: Dict = {}
        self.logins: Dict[str, float] = {}       # repeater key -> time logged in
        self._load()

    # ------------------------------------------------------------ messages
    def _load(self) -> None:
        if not MSG_FILE.exists():
            return
        try:
            lines = MSG_FILE.read_text(encoding="utf-8").splitlines()[-KEEP:]
        except OSError:
            log.exception("web: could not read %s", MSG_FILE)
            return
        for line in lines:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            self.seq += 1
            m["id"] = self.seq
            self.msgs.append(m)
        # keep the file from growing without end
        if len(lines) == KEEP:
            try:
                MSG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError:
                log.exception("web: could not trim %s", MSG_FILE)

    def record(self, direction: str, kind: str, text: str, peer: str = "",
               name: str = "", channel: Optional[int] = None, **extra) -> None:
        """Remember one mesh message (in or out) for the Messages tab."""
        self.seq += 1
        m = {"t": round(time.time(), 1), "dir": direction, "kind": kind,
             "peer": peer, "name": name, "ch": channel, "text": text}
        m.update({k: v for k, v in extra.items() if v is not None})
        try:
            MSG_FILE.parent.mkdir(exist_ok=True)
            with MSG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(m) + "\n")
        except OSError:
            log.exception("web: could not append to %s", MSG_FILE)
        m["id"] = self.seq
        self.msgs.append(m)

    def contact_name(self, prefix: str) -> str:
        mc = self.bridge.mc if self.bridge else None
        if mc and prefix:
            c = mc.get_contact_by_key_prefix(prefix)
            if c:
                return c.get("adv_name", "")
        return ""

    def attach(self, bridge) -> None:
        """Called once the bridge has connected to the radio."""
        self.bridge = bridge
        mc = bridge.mc

        async def on_dm(ev):
            p = ev.payload or {}
            kp = str(p.get("pubkey_prefix", ""))
            self.record("in", "dm", str(p.get("text", "")), peer=kp,
                        name=self.contact_name(kp), snr=p.get("SNR"),
                        hops=p.get("path_len"))

        async def on_chan(ev):
            p = ev.payload or {}
            name, sep, body = str(p.get("text", "")).partition(": ")
            if not sep:
                name, body = "", str(p.get("text", ""))
            self.record("in", "chan", body, name=name, channel=p.get("channel_idx"),
                        snr=p.get("SNR"), hops=p.get("path_len"))

        mc.subscribe(EventType.CONTACT_MSG_RECV, on_dm)
        mc.subscribe(EventType.CHANNEL_MSG_RECV, on_chan)

    # ------------------------------------------------------------ server
    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", self.port)
        log.info("radio web page on http://127.0.0.1:%d", self.port)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = (lines[0].split(" ") + ["", ""])[:3]
            hdrs = {}
            for l in lines[1:]:
                k, _, v = l.partition(":")
                if k:
                    hdrs[k.strip().lower()] = v.strip()
            n = int(hdrs.get("content-length") or 0)
            if n > 65536:
                raise ApiError(413, "request too large")
            body = await asyncio.wait_for(reader.readexactly(n), 15) if n else b""
            status, ctype, out = await self._route(method, target, hdrs, body)
        except ApiError as e:
            status, ctype, out = e.status, "application/json", json.dumps({"error": str(e)}).encode()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        except Exception as e:
            log.exception("web: request failed")
            status, ctype = 500, "application/json"
            out = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                  413: "Too Large", 502: "Bad Gateway", 503: "Unavailable",
                  504: "Timeout"}.get(status, "Error")
        try:
            writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
                         f"Content-Length: {len(out)}\r\nCache-Control: no-store\r\n"
                         "X-Frame-Options: DENY\r\nConnection: close\r\n\r\n".encode() + out)
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _route(self, method: str, target: str, hdrs: Dict, body: bytes):
        host = hdrs.get("host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            raise ApiError(403, "local access only")
        u = urlsplit(target)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if method == "GET" and u.path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", PAGE.read_bytes()
        if not u.path.startswith("/api/"):
            raise ApiError(404, "not found")
        name = u.path[5:].replace("/", "_").replace("-", "_")
        if method == "POST":
            if hdrs.get("x-radio") != "1":
                raise ApiError(403, "missing X-Radio header")
            try:
                args = json.loads(body or b"{}")
            except ValueError:
                raise ApiError(400, "body is not JSON")
            fn = getattr(self, "post_" + name, None)
        elif method == "GET":
            args, fn = q, getattr(self, "get_" + name, None)
        else:
            raise ApiError(400, "GET or POST only")
        if fn is None:
            raise ApiError(404, f"no API {u.path}")
        res = await fn(args)
        return 200, "application/json", json.dumps(_jsonable(res)).encode()

    # ------------------------------------------------------------ helpers
    def _mc(self):
        b = self.bridge
        if not b or not b.mc or not b.mc.is_connected:
            raise ApiError(503, "radio not connected (bridge reconnecting?)")
        return b.mc

    async def _cmd(self, coro_fn, timeout: float = 20):
        """Run one radio command, in turn with the bridge's own traffic."""
        mc = self._mc()
        async with self.bridge.radio_lock:
            return await asyncio.wait_for(coro_fn(mc), timeout)

    def _contact(self, key: str) -> Dict:
        mc = self._mc()
        key = (key or "").lower()
        c = mc.contacts.get(key) or mc.get_contact_by_key_prefix(key[:12])
        if not c:
            raise ApiError(404, f"no contact {key[:12]}")
        return c

    # ------------------------------------------------------------ GET
    async def get_state(self, q) -> Dict:
        b = self.bridge
        connected = bool(b and b.mc and b.mc.is_connected)
        out = {"connected": connected, "port": b.cfg["radio"]["port"] if b else "",
               "bridge_channel": b.chan if b else None, "device": self.device}
        if connected:
            out["self"] = b.mc.self_info
            out["sessions"] = self._sessions()
            out["members"] = b.members
            out["sysops"] = {n: u.get("pubkey", "")[:12] for n, u in b.cfg["users"].items()}
        return out

    def _sessions(self):
        rows = []
        for name, st in self.bridge.users.items():
            s = st.session
            rows.append({"name": name, "dest": st.dest or "channel",
                         "open": bool(s and not s.closed),
                         "idle_s": int(time.monotonic() - s.last_used) if s and not s.closed else None,
                         "pages_waiting": len(st.pending)})
        return rows

    async def get_messages(self, q) -> Dict:
        since = int(q.get("since") or 0)
        return {"messages": [m for m in self.msgs if m["id"] > since]}

    async def get_contacts(self, q) -> Dict:
        await self._cmd(lambda mc: mc.ensure_contacts(follow=True))
        mc = self._mc()
        rows = []
        for k, c in mc.contacts.items():
            rows.append({"key": k, "name": c.get("adv_name", ""),
                         "type": ADV_TYPES.get(c.get("type"), str(c.get("type"))),
                         "type_id": c.get("type"),
                         "last_advert": c.get("last_advert"),
                         "hops": c.get("out_path_len"), "path": c.get("out_path", ""),
                         "lat": c.get("adv_lat"), "lon": c.get("adv_lon")})
        rows.sort(key=lambda r: -(r["last_advert"] or 0))
        return {"contacts": rows, "pending": list(mc.pending_contacts.keys())}

    async def get_channels(self, q) -> Dict:
        n = int(self.device.get("max_channels") or 8)
        rows = []
        for i in range(n):
            try:
                p = _ok(await self._cmd(lambda mc, i=i: mc.commands.get_channel(i), 8))
            except ApiError:
                continue
            name = p.get("channel_name", "")
            secret = p.get("channel_secret") or b""
            if not name and not any(secret):
                continue
            rows.append({"idx": i, "name": name, "hash": p.get("channel_hash")})
        return {"channels": rows, "bridge_channel": self.bridge.chan}

    async def get_stats(self, q) -> Dict:
        out = {}
        for key, fn in (("battery", lambda mc: mc.commands.get_bat()),
                        ("core", lambda mc: mc.commands.get_stats_core()),
                        ("radio", lambda mc: mc.commands.get_stats_radio()),
                        ("packets", lambda mc: mc.commands.get_stats_packets())):
            try:
                out[key] = _ok(await self._cmd(fn, 8))
            except ApiError as e:
                out[key] = {"error": str(e)}
        return out

    # ------------------------------------------------------------ POST
    async def post_send(self, a) -> Dict:
        text = str(a.get("text") or "").strip()
        if not text:
            raise ApiError(400, "nothing to send")
        if len(text.encode()) > 140:
            raise ApiError(400, "too long for one mesh message (140 bytes)")
        if a.get("to"):
            c = self._contact(a["to"])
            r = await self._cmd(lambda mc: mc.commands.send_msg_with_retry(c, text), 90)
            acked = r is not None and r.type != EventType.ERROR
            self.record("out", "dm", text, peer=c["public_key"][:12],
                        name=c.get("adv_name", ""), via="web", acked=acked)
            return {"acked": acked}
        ch = int(a.get("channel", 0))
        _ok(await self._cmd(lambda mc: mc.commands.send_chan_msg(ch, text)))
        self.record("out", "chan", text, channel=ch, name="(this radio)", via="web")
        return {"sent": True}

    async def post_advert(self, a) -> Dict:
        flood = bool(a.get("flood"))
        _ok(await self._cmd(lambda mc: mc.commands.send_advert(flood=flood)))
        log.info("web: %s advert sent", "flood" if flood else "zero-hop")
        return {"sent": True}

    async def post_settings(self, a) -> Dict:
        done = []
        if a.get("name") is not None:
            name = str(a["name"]).strip()
            if not name:
                raise ApiError(400, "name is empty")
            _ok(await self._cmd(lambda mc: mc.commands.set_name(name)))
            done.append("name")
        if a.get("lat") is not None and a.get("lon") is not None:
            lat, lon = float(a["lat"]), float(a["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ApiError(400, "lat/lon out of range")
            _ok(await self._cmd(lambda mc: mc.commands.set_coords(lat, lon)))
            done.append("position")
        if a.get("tx_power") is not None:
            p = int(a["tx_power"])
            mx = int((self._mc().self_info or {}).get("max_tx_power") or 30)
            if not (1 <= p <= mx):
                raise ApiError(400, f"TX power must be 1-{mx} dBm")
            _ok(await self._cmd(lambda mc: mc.commands.set_tx_power(p)))
            done.append("tx power")
        if a.get("manual_add") is not None:
            v = bool(a["manual_add"])
            _ok(await self._cmd(lambda mc: mc.commands.set_manual_add_contacts(v)))
            done.append("auto-add")
        if a.get("radio"):
            r = a["radio"]
            if a.get("confirm") != "CHANGE RADIO":
                raise ApiError(400, "radio change not confirmed")
            f, bw, sf, cr = float(r["freq"]), float(r["bw"]), int(r["sf"]), int(r["cr"])
            if not (137 <= f <= 1020 and 5 <= sf <= 12 and 5 <= cr <= 8 and 7 <= bw <= 500):
                raise ApiError(400, "radio values out of range")
            _ok(await self._cmd(lambda mc: mc.commands.set_radio(f, bw, sf, cr)))
            done.append("radio")
        if not done:
            raise ApiError(400, "nothing to change")
        await self._cmd(lambda mc: mc.commands.send_appstart())   # refresh self_info
        log.info("web: radio settings changed: %s", ", ".join(done))
        return {"changed": done, "self": self._mc().self_info}

    async def post_sync_time(self, a) -> Dict:
        _ok(await self._cmd(lambda mc: mc.commands.set_time(int(time.time()))))
        return {"time": int(time.time())}

    async def post_reboot(self, a) -> Dict:
        if a.get("confirm") != "REBOOT":
            raise ApiError(400, "reboot not confirmed")
        log.warning("web: radio reboot requested; the bridge will reconnect")
        mc = self._mc()
        async with self.bridge.radio_lock:
            try:
                await asyncio.wait_for(mc.commands.reboot(), 3)
            except Exception:
                pass          # the radio goes away without answering
        return {"rebooting": True}

    async def post_contact_reset_path(self, a) -> Dict:
        c = self._contact(a.get("key"))
        _ok(await self._cmd(lambda mc: mc.commands.reset_path(c["public_key"])))
        return {"done": True}

    async def post_contact_remove(self, a) -> Dict:
        c = self._contact(a.get("key"))
        _ok(await self._cmd(lambda mc: mc.commands.remove_contact(c["public_key"])))
        self._mc().contacts.pop(c["public_key"], None)
        log.info("web: removed contact %s (%s)", c.get("adv_name"), c["public_key"][:12])
        return {"done": True}

    async def post_contact_add_pending(self, a) -> Dict:
        mc = self._mc()
        c = mc.pop_pending_contact(str(a.get("key") or ""))
        if not c:
            raise ApiError(404, "no such pending contact")
        _ok(await self._cmd(lambda mc: mc.commands.add_contact(c)))
        return {"done": True}

    async def post_channel(self, a) -> Dict:
        i = int(a.get("idx"))
        name = str(a.get("name") or "").strip()
        if i == self.bridge.chan and a.get("confirm") != "BRIDGE CHANNEL":
            raise ApiError(400, f"channel {i} is the bridge's private channel")
        secret = a.get("secret")
        sb = None
        if secret:
            try:
                sb = bytes.fromhex(str(secret))
            except ValueError:
                raise ApiError(400, "secret must be 32 hex characters")
            if len(sb) != 16:
                raise ApiError(400, "secret must be 32 hex characters")
        if not name:             # clear the slot
            name, sb = "", bytes(16)
        _ok(await self._cmd(lambda mc: mc.commands.set_channel(i, name, sb)))
        log.info("web: channel %d set to %r", i, name)
        return {"done": True}

    async def post_repeater_login(self, a) -> Dict:
        c = self._contact(a.get("key"))
        pwd = str(a.get("password") or "")
        r = await self._cmd(lambda mc: mc.commands.send_login_sync(c, pwd, timeout=20), 30)
        if r is None:
            raise ApiError(504, "no answer from that node")
        if r.type == EventType.LOGIN_FAILED:
            raise ApiError(403, "login refused (wrong password?)")
        _ok(r)
        self.logins[c["public_key"]] = time.time()
        return {"logged_in": True, "payload": r.payload}

    async def post_repeater_status(self, a) -> Dict:
        c = self._contact(a.get("key"))
        r = await self._cmd(lambda mc: mc.commands.req_status_sync(c, timeout=20), 30)
        if r is None:
            raise ApiError(504, "no answer from that node")
        return {"status": r.payload if hasattr(r, "payload") else r}

    async def post_repeater_cmd(self, a) -> Dict:
        c = self._contact(a.get("key"))
        cmd = str(a.get("cmd") or "").strip()
        if not cmd:
            raise ApiError(400, "no command")
        mc = self._mc()
        reply = asyncio.get_running_loop().create_future()
        pk = c["public_key"][:12]

        async def got(ev):
            p = ev.payload or {}
            if p.get("pubkey_prefix") == pk and p.get("txt_type") == 1 and not reply.done():
                reply.set_result(p.get("text", ""))
        sub = mc.subscribe(EventType.CONTACT_MSG_RECV, got)
        try:
            _ok(await self._cmd(lambda mc: mc.commands.send_cmd(c, cmd)))
            try:
                text = await asyncio.wait_for(reply, 25)
            except asyncio.TimeoutError:
                text = None
        finally:
            mc.unsubscribe(sub)
        log.info("web: CLI %r to %s -> %r", cmd, c.get("adv_name"), (text or "")[:60])
        if text is None:
            raise ApiError(504, "sent, but no reply within 25 s (logged in?)")
        return {"reply": text}

    async def post_member(self, a) -> Dict:
        cmd = str(a.get("action") or "").upper()
        call = str(a.get("callsign") or "").upper()
        if cmd not in ("APPROVE", "DENY"):
            raise ApiError(400, "action must be APPROVE or DENY")
        if not self.bridge.decide(cmd, call, "web page"):
            raise ApiError(404, f"no signup for {call}")
        asyncio.create_task(self.bridge.tell_decision(cmd, call))
        return {"done": True, "members": self.bridge.members}
