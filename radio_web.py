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
import hashlib
import json
import logging
import os
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
TALK_S = 30 * 60     # talk mode turns itself off after this long
RX_MATCH_S = 15      # how far back a received DM is matched to its RF log entry


def msg_hash(ts: int, text: str) -> int:
    """The meshcore library's id for a channel message: its sender timestamp
    plus the full 'name: text' body. Every copy of the same post (each repeat
    we hear) has the same hash, so repeats can be counted."""
    d = hashlib.sha256(int(ts).to_bytes(4, "little") + text.encode("utf-8")).digest()
    return int.from_bytes(d[:4], "little")


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
        self.seq = 0                             # message ids
        self.ver = 0                             # bumped on every new or changed message
        self.by_hash: Dict[int, Dict] = {}       # channel msg_hash -> message
        self.rxlog: deque = deque(maxlen=400)    # recent RF log entries (every packet heard)
        self.server: Optional[asyncio.base_events.Server] = None
        self.device: Dict = {}
        self.logins: Dict[str, float] = {}       # repeater key -> time logged in
        # Talk mode: key prefix (12 hex) -> time.time() it ends. While on, DMs
        # from that contact come to the page and the gateway ignores them.
        self.talk: Dict[str, float] = {}
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
        by_t: Dict[float, Dict] = {}
        for line in lines:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "upd" in m:                       # later details for an earlier message
                old = by_t.get(m.pop("upd"))
                if old:
                    old.update(m)
                continue
            self.seq += 1
            self.ver += 1
            m["id"], m["v"] = self.seq, self.ver
            by_t[m["t"]] = m
            if m.get("mh") is not None:
                self.by_hash[m["mh"]] = m
            self.msgs.append(m)
        # keep the file from growing without end
        if len(lines) == KEEP:
            try:
                MSG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError:
                log.exception("web: could not trim %s", MSG_FILE)

    def record(self, direction: str, kind: str, text: str, peer: str = "",
               name: str = "", channel: Optional[int] = None, **extra) -> Dict:
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
        self.ver += 1
        m["id"], m["v"] = self.seq, self.ver
        self.msgs.append(m)
        if m.get("mh") is not None:
            self.by_hash[m["mh"]] = m
            while len(self.by_hash) > KEEP:
                del self.by_hash[next(iter(self.by_hash))]
            heard = [self._heard_row(e) for e in self.rxlog if e.get("mh") == m["mh"]]
            if heard and direction == "out":
                self.update(m, heard=heard)
            elif heard:
                self.update(m, copies=heard)
        return m

    def update(self, m: Dict, **fields) -> None:
        """Add details to a message already recorded (saved as an update line)."""
        m.update(fields)
        self.ver += 1
        m["v"] = self.ver
        try:
            with MSG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"upd": m["t"], **fields}) + "\n")
        except OSError:
            log.exception("web: could not append to %s", MSG_FILE)

    @staticmethod
    def _heard_row(e: Dict) -> Dict:
        return {k: e.get(k) for k in ("t", "snr", "rssi", "path", "phs", "route")}

    def on_rx(self, p: Dict) -> None:
        """One packet heard on the air (the radio's RF log)."""
        e = {"t": p.get("recv_time") or time.time(), "snr": p.get("snr"), "rssi": p.get("rssi"),
             "route": p.get("route_typename"), "ptype": p.get("payload_type"),
             "path": p.get("path") or "", "phs": p.get("path_hash_size") or 1,
             "hops": p.get("path_len")}
        if p.get("msg_hash") is not None:
            e["mh"] = p["msg_hash"]
        pl = p.get("pkt_payload") or b""
        if e["ptype"] == 2 and len(pl) >= 2:     # TXT_MSG (a DM): dest hash, src hash
            e["dst"], e["src"] = pl[:1].hex(), pl[1:2].hex()
        self.rxlog.append(e)
        m = self.by_hash.get(e.get("mh"))
        if m is None:
            return
        key = "heard" if m["dir"] == "out" else "copies"
        rows = list(m.get(key) or [])
        if any(r.get("path") == e["path"] for r in rows):
            return                                 # same copy heard again
        rows.append(self._heard_row(e))
        self.update(m, **{key: rows})

    def _rx_for_dm(self, prefix: str) -> Optional[Dict]:
        """The RF log entry of a DM just received from `prefix`, if we can tell."""
        me = ((self.bridge.mc.self_info or {}).get("public_key") or "")[:2]
        src, now = (prefix or "")[:2].lower(), time.time()
        for e in reversed(self.rxlog):
            if now - e["t"] > RX_MATCH_S:
                break
            if e.get("src") == src and (not me or e.get("dst") == me):
                return e
        return None

    def talking(self, prefix: str) -> bool:
        """True while DMs from this key prefix are for the page, not the gateway."""
        kp = (prefix or "").lower()[:12]
        end = self.talk.get(kp)
        if end is None:
            return False
        if end < time.time():
            del self.talk[kp]
            log.info("web: talk mode with %s ended (timed out)", kp)
            return False
        return True

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
        # Let the library decrypt channel packets in the RF log, so each post's
        # path and every repeat of it can be matched. It needs the channel keys,
        # which it learns from get_channel replies.
        mc.set_decrypt_channel_logs(True)
        asyncio.get_running_loop().create_task(self._learn_channels())

        async def on_dm(ev):
            p = ev.payload or {}
            kp = str(p.get("pubkey_prefix", ""))
            rx = self._rx_for_dm(kp) or {}
            self.record("in", "dm", str(p.get("text", "")), peer=kp,
                        name=self.contact_name(kp), snr=p.get("SNR"),
                        hops=p.get("path_len"), sent_ts=p.get("sender_timestamp"),
                        rssi=rx.get("rssi"), path=rx.get("path"), phs=rx.get("phs"),
                        route=rx.get("route"),
                        talk=True if self.talking(kp) else None)

        async def on_chan(ev):
            p = ev.payload or {}
            name, sep, body = str(p.get("text", "")).partition(": ")
            if not sep:
                name, body = "", str(p.get("text", ""))
            ts = p.get("sender_timestamp")
            self.record("in", "chan", body, name=name, channel=p.get("channel_idx"),
                        snr=p.get("SNR"), hops=p.get("path_len"), sent_ts=ts,
                        rssi=p.get("RSSI"), path=p.get("path"),
                        phs=(p.get("path_hash_mode") or 0) + 1,
                        mh=msg_hash(ts, str(p.get("text", ""))) if ts else None)

        async def on_rx(ev):
            try:
                self.on_rx(ev.payload or {})
            except Exception:
                log.exception("web: RF log entry not understood")

        mc.subscribe(EventType.CONTACT_MSG_RECV, on_dm)
        mc.subscribe(EventType.CHANNEL_MSG_RECV, on_chan)
        mc.subscribe(EventType.RX_LOG_DATA, on_rx)

    async def _learn_channels(self) -> None:
        await asyncio.sleep(3)                  # let the bridge finish connecting
        n = int(self.device.get("max_channels") or 8)
        for i in range(n):
            try:
                await self._cmd(lambda mc, i=i: mc.commands.get_channel(i), 8)
            except Exception as e:
                log.warning("web: could not read channel %d for the RF log: %s", i, e)

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
        now = time.time()
        out["talk"] = {k: int(e - now) for k, e in self.talk.items() if e > now}
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
        since = int(q.get("since") or 0)       # a version number, not an id
        return {"messages": [m for m in self.msgs if m["v"] > since], "ver": self.ver}

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
            kp = c["public_key"][:12].lower()
            if kp not in self.talk:
                log.info("web: talk mode with %s (%s) on", kp, c.get("adv_name", ""))
            self.talk[kp] = time.time() + TALK_S   # before sending: replies can be quick
            r = await self._cmd(lambda mc: mc.commands.send_msg_with_retry(c, text), 90)
            acked = r is not None and r.type != EventType.ERROR
            hops = c.get("out_path_len")
            self.record("out", "dm", text, peer=c["public_key"][:12],
                        name=c.get("adv_name", ""), via="web", acked=acked,
                        hops=hops, path=c.get("out_path") if hops and hops > 0 else None,
                        phs=(c.get("out_path_hash_mode") or 0) + 1 if hops and hops > 0 else None,
                        route="DIRECT" if hops is not None and hops >= 0 else "FLOOD")
            return {"acked": acked}
        ch = int(a.get("channel", 0))
        ts = int(time.time())     # ours, so we know the hash its repeats will carry
        _ok(await self._cmd(lambda mc: mc.commands.send_chan_msg(ch, text, ts)))
        me = (self._mc().self_info or {}).get("name", "")
        self.record("out", "chan", text, channel=ch, name="(this radio)", via="web",
                    sent_ts=ts, mh=msg_hash(ts, f"{me}: {text}"), heard=[])
        return {"sent": True}

    async def post_talk(self, a) -> Dict:
        """The page's switch: on holds replies for TALK_S (then it turns itself
        off); off hands the contact back to the gateway now."""
        kp = str(a.get("key") or "").lower()[:12]
        if len(kp) != 12:
            raise ApiError(400, "key must be at least 12 hex characters")
        if a.get("on"):
            self.talk[kp] = time.time() + TALK_S
            log.info("web: talk mode with %s switched on", kp)
        elif self.talk.pop(kp, None) is not None:
            log.info("web: talk mode with %s off; the gateway answers again", kp)
        return {"talk": kp in self.talk}

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
        if a.get("share_loc") is not None:
            v = 1 if a["share_loc"] else 0
            _ok(await self._cmd(lambda mc: mc.commands.set_advert_loc_policy(v)))
            done.append("position " + ("shared" if v else "private"))
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
        if a.get("path_hash_mode") is not None:
            m = int(a["path_hash_mode"])
            if m not in (0, 1, 2):
                raise ApiError(400, "path hash size must be 1, 2 or 3 bytes")
            _ok(await self._cmd(lambda mc: mc.commands.set_path_hash_mode(m)))
            dq = await self._cmd(lambda mc: mc.commands.send_device_query())
            if dq is not None and dq.type != EventType.ERROR:
                self.device = dq.payload
            done.append(f"path hash {m + 1} byte(s)")
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
        secret = str(a.get("secret") or "").strip()
        if a.get("clear") or not name:
            name, sb = "", bytes(16)                 # empty slot
        elif secret:
            try:
                sb = bytes.fromhex(secret)
            except ValueError:
                sb = b""
            if len(sb) != 16:
                raise ApiError(400, "secret must be 32 hex characters")
        elif name.startswith("#"):
            sb = None                                 # key derived from the name
        else:
            # Private channel, no key given: keep the slot's key, or make one.
            cur = await self._cmd(lambda mc: mc.commands.get_channel(i), 8)
            old = (cur.payload or {}).get("channel_secret") if cur and cur.type != EventType.ERROR else None
            sb = old if old and any(old) else os.urandom(16)
        _ok(await self._cmd(lambda mc: mc.commands.set_channel(i, name, sb)))
        log.info("web: channel %d set to %r", i, name)
        await self._cmd(lambda mc: mc.commands.get_channel(i), 8)   # RF log learns the key
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
