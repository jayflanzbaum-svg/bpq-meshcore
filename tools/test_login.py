"""Log into BPQ telnet with each user in meshcore_config.json, run ? and
BYE at the node, and print what the mesh would get (after the display
rewrites). No radio needed."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bpq_meshcore as b  # noqa: E402

cfg = json.loads((ROOT / "meshcore_config.json").read_text(encoding="utf-8"))
for k in ("bbs", "display"):
    cfg[k] = {**b.DEFAULT_CONFIG[k], **cfg.get(k, {})}


async def show(text, closed):
    for c in b.chunk_text(text, b.DEFAULT_CONFIG["paging"]["chunk_chars"]):
        print(f"[{len(c):3}] {c}")
    if closed:
        print("[closed]")


async def one(name, user):
    print(f"=== {name} as {user['callsign']} ===")
    s = b.NodeSession(cfg, user, name, show)
    await s.open()
    for cmd in ("?", "BYE"):
        print(f">>> {cmd}")
        await s.send_line(cmd)
        await asyncio.sleep(3)
    await s.close()


async def main():
    for name, user in cfg["users"].items():
        await one(name, user)

asyncio.run(main())
