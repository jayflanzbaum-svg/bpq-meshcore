"""Log into BPQ telnet with each user in meshcore_config.json, run ? and
BYE at the node, print what comes back. No radio needed. Run after applying
the telnet USER line."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bpq_meshcore as b  # noqa: E402

cfg = json.loads((ROOT / "meshcore_config.json").read_text(encoding="utf-8"))
cfg["bbs"] = {**b.DEFAULT_CONFIG["bbs"], **cfg.get("bbs", {})}


async def show(text, closed):
    print(text + ("\n[closed]" if closed else ""))


async def one(name, user):
    print(f"=== {name} as {user['callsign']} ===")
    s = b.NodeSession(cfg, user, name, show)
    await s.open()
    await asyncio.sleep(3)
    for cmd in ("?", "BYE"):
        print(f">>> {cmd}")
        await s.send_line(cmd)
        await asyncio.sleep(3)
    await s.close()


async def main():
    for name, user in cfg["users"].items():
        await one(name, user)

asyncio.run(main())
