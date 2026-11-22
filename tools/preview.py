"""Preview what mesh users get from this gateway, with no radio.

Runs the bridge's own reply code (node login, display rewrites, paging,
"(1/3)" part markers, M=More, the guest menu) against your BPQ telnet port
and prints each message instead of sending it, with its size and airtime.
Use it to see what a command costs on the air, then trim with the
"display" and "paging" settings in meshcore_config.json and run it again.
The bridge itself can keep running: this never opens the radio's COM port.

  py tools\\preview.py                       interactive, as the first user
  py tools\\preview.py --as N0CALL-Phone     as another entry in "users"
  py tools\\preview.py --guest               as a guest radio (first contact)
  py tools\\preview.py ? BBS L BYE           run these commands, then exit
  py tools\\preview.py --config other.json --chunk 100 ?

A user preview logs in to BPQ as that user, like the mesh would. A guest
preview connects to the guest apps as GUEST000000. MYCALL signups and
APPROVE/DENY are shown but not carried out, so members and sysops are
not touched.
"""
import argparse
import asyncio
import logging
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bpq_meshcore as b  # noqa: E402

GUEST_KEY = "000000000000"


def airtime_ms(text_bytes: int, sf: int, bw_khz: float, cr: int, hops: int = 0,
               hash_size: int = 2) -> float:
    """Rough LoRa time on air for one DM carrying `text_bytes` of text: one
    send, no repeats, no ACK. Packet: header, path length, path, dest and
    source hashes, MAC, then timestamp+flags+text padded to 16 (AES)."""
    pl = 1 + 1 + hops * hash_size + 2 + 2 + 16 * math.ceil((5 + text_bytes) / 16)
    tsym = (2 ** sf) / (bw_khz * 1000) * 1000          # ms
    de = 1 if tsym > 16 else 0
    n = 8 + max(math.ceil((8 * pl - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * cr, 0)
    return (8 + 4.25) * tsym + n * tsym


class PreviewBridge(b.Bridge):
    """The real Bridge with the radio swapped for print()."""

    def __init__(self, cfg, a):
        super().__init__(cfg)
        self.a = a
        self.count = self.bytes = 0
        self.air = 0.0
        self.last_out = time.monotonic()

    async def say(self, text, dest=None):
        n = len(text.encode("utf-8"))
        self.count += 1
        self.bytes += n
        ms = airtime_ms(n, self.a.sf, self.a.bw, self.a.cr)
        self.air += ms
        self.last_out = time.monotonic()
        flag = "  << OVER the radio's limit, will be cut" if n > b.MSG_MAX else ""
        print(f"--- msg {self.count}: {n}/{b.MSG_MAX} bytes, ~{ms / 1000:.1f} s air{flag}")
        for line in text.split("\n"):
            print("    " + line)

    async def admin_command(self, name, body, dest):
        if (body.split() or [""])[0].upper() in ("APPROVE", "DENY", "PENDING"):
            print("    (preview: sysop command not carried out)")
            return True
        return False

    async def signup(self, name, kp, args):
        print("    (preview: MYCALL signup not carried out; the real bridge checks "
              "the callsign with the FCC and asks the sysop)")

    def summary(self, st):
        if not self.count:
            print("=== no reply")
        else:
            print(f"=== {self.count} message{'s' if self.count != 1 else ''}, "
                  f"{self.bytes} bytes, ~{self.air / 1000:.1f} s of airtime "
                  f"(one send each; repeats, retries and ACKs add more)")
        if st and st.pending:
            n = self.cfg["paging"]["chunks_per_reply"]
            print(f"    {len(st.pending)} more message(s) wait for M "
                  f"({-(-len(st.pending) // n)} page(s))")
        self.count = self.bytes = 0
        self.air = 0.0


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("commands", nargs="*", help="commands to run, then exit")
    ap.add_argument("--config", default=str(ROOT / "meshcore_config.json"))
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--as", dest="user", help='an entry in "users" (default: the first)')
    who.add_argument("--guest", action="store_true", help="preview as a guest radio")
    ap.add_argument("--chunk", type=int, help="try another paging.chunk_chars")
    ap.add_argument("--per-reply", type=int, help="try another paging.chunks_per_reply")
    ap.add_argument("--wait", type=float, default=6,
                    help="seconds of silence that end a command's reply (default 6; "
                         "AI apps may need 30)")
    ap.add_argument("--sf", type=int, default=7, help="spreading factor (default 7)")
    ap.add_argument("--bw", type=float, default=62.5, help="bandwidth kHz (default 62.5)")
    ap.add_argument("--cr", type=int, default=5, help="coding rate 4/x (default 5)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the bridge's log")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="    [log] %(levelname)s %(message)s")
    b.CONFIG_FILE = Path(a.config)
    cfg = b.load_config()
    if a.chunk:
        cfg["paging"]["chunk_chars"] = a.chunk
    if a.per_reply:
        cfg["paging"]["chunks_per_reply"] = a.per_reply
    br = PreviewBridge(cfg, a)

    if a.guest:
        st_key = "dm:" + GUEST_KEY
        send = lambda body: br.dispatch_guest(GUEST_KEY, body)  # noqa: E731
        print("Previewing as a guest radio (GUEST000000). The first message gets "
              "the welcome, whatever it says.")
    else:
        if not cfg["users"]:
            sys.exit('No "users" in the config; use --guest.')
        name = a.user or next(iter(cfg["users"]))
        if name not in cfg["users"]:
            sys.exit(f"No user {name!r}. Users: {', '.join(cfg['users'])}")
        st_key = name
        send = lambda body: br.dispatch(name, "preview", body)  # noqa: E731
        print(f"Previewing as {name} ({cfg['users'][name].get('callsign')}), "
              "logged in to BPQ like a mesh DM.")
    p = cfg["paging"]
    print(f"chunk_chars {p['chunk_chars']}, chunks_per_reply {p['chunks_per_reply']}, "
          f"max_pages {p['max_pages']}; airtime at SF{a.sf} BW{a.bw:g} CR4/{a.cr}\n")

    async def run(body: str) -> None:
        print(f">>> {body}")
        br.last_out = time.monotonic()
        await send(body)
        while time.monotonic() - br.last_out < a.wait:   # until BPQ goes quiet
            await asyncio.sleep(0.25)
        br.summary(br.users.get(st_key))
        print()

    try:
        if a.commands:
            for c in a.commands:
                await run(c)
        else:
            print("Type what a mesh user would send. Empty line or Ctrl+C quits.")
            while True:
                line = await asyncio.to_thread(input, "mesh> ")
                if not line.strip():
                    break
                await run(line.strip())
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        for st in br.users.values():
            if st.session and not st.session.closed:
                await st.session.close()


if __name__ == "__main__":
    asyncio.run(main())
