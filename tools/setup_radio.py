"""
One-time radio setup for bpq_meshcore.py.

  py tools\\setup_radio.py COM9 "MYCALL BBS"            # name + US preset + private channel 1
  py tools\\setup_radio.py COM9 "MYCALL BBS" --slot 2 --no-radio

What it does, on a radio already flashed with MeshCore *USB Serial Companion*:
  1. sets the node name (what users see as the sender);
  2. sets the radio to the USA/Canada recommended preset (910.525 MHz,
     62.5 kHz, SF7, CR5) unless --no-radio - outside North America use
     --freq/--bw/--sf/--cr or set it in the MeshCore app instead;
  3. creates a PRIVATE channel with a random 16-byte key (never a #hashtag
     channel: its key is derived from the name, so anyone can read it);
  4. writes the key and a join link to channel-secrets\\channel.txt and,
     if `qrcode[pil]` is installed, a QR code users scan in the MeshCore app.

channel-secrets\\ is gitignored. Anyone with that key can read the channel
and post under any name, so share the QR only with people you would give a
BBS login to.
"""
import argparse
import asyncio
import secrets
import sys
import urllib.parse
from pathlib import Path

from meshcore import EventType, MeshCore

OUT = Path(__file__).resolve().parent.parent / "channel-secrets"


async def main(a) -> int:
    mc = await MeshCore.create_serial(a.port, 115200)
    if mc is None:
        print(f"No MeshCore companion answered on {a.port}. Is it the USB companion firmware?")
        return 1
    try:
        steps = [("name", mc.commands.set_name(a.name))]
        if not a.no_radio:
            steps.append(("radio", mc.commands.set_radio(a.freq, a.bw, a.sf, a.cr)))
        key = secrets.token_bytes(16)
        steps.append(("channel", mc.commands.set_channel(a.slot, a.name, key)))
        for label, coro in steps:
            r = await coro
            if r.type == EventType.ERROR:
                print(f"{label}: FAILED {r.payload}")
                return 1
            print(f"{label}: ok")
    finally:
        await mc.disconnect()

    OUT.mkdir(exist_ok=True)
    uri = "meshcore://channel/add?name={}&secret={}".format(
        urllib.parse.quote(a.name), key.hex())
    (OUT / "channel.txt").write_text(
        f"name={a.name}\nslot={a.slot}\nsecret={key.hex()}\nuri={uri}\n", encoding="utf-8")
    print(f"Key saved to {OUT / 'channel.txt'}")
    try:
        import qrcode
        png = OUT / "join-channel.png"
        qrcode.make(uri, box_size=10, border=4).save(png)
        print(f"Join QR code: {png}")
    except ImportError:
        print("pip install qrcode[pil] for a QR code; or share the uri= line.")
    print(f'Set "channel": {{"index": {a.slot}}} in meshcore_config.json.')
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("port", help="COM port of the MeshCore radio, e.g. COM9")
    p.add_argument("name", help='node and channel name, e.g. "N0CALL BBS"')
    p.add_argument("--slot", type=int, default=1, help="channel slot (0 is Public; default 1)")
    p.add_argument("--no-radio", action="store_true", help="leave the radio settings alone")
    p.add_argument("--freq", type=float, default=910.525)
    p.add_argument("--bw", type=float, default=62.5)
    p.add_argument("--sf", type=int, default=7)
    p.add_argument("--cr", type=int, default=5)
    a = p.parse_args()
    if a.slot == 0:
        sys.exit("Slot 0 is the Public channel - pick another slot.")
    sys.exit(asyncio.run(main(a)))
