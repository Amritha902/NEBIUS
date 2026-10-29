"""Build a server-free replay page for GitHub Pages.

    python scripts/export_static.py [web/replays/NAME.jsonl] [--out docs/demo/index.html]

Inlines a recorded run into the web UI and swaps the API for an in-page
player, so the page loads instantly from any static host -- no cold start, no
model credits -- and is labelled REPLAY exactly as the live server labels it.
Picks the newest live recording when no file is given, else the newest one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLAYER = r"""
<script>
// Static replay build: the API is served from the recording embedded below.
(() => {
  const S = window.__STATIC__;
  const realFetch = window.fetch.bind(window);
  const json = obj => Promise.resolve(new Response(JSON.stringify(obj), {headers: {"Content-Type": "application/json"}}));
  window.fetch = (url, opts) => {
    const u = String(url);
    if (u.startsWith("/api/status")) return json(S.status);
    if (u.startsWith("/api/replays")) return json(S.replays);
    return realFetch(url, opts);
  };
  class ReplaySource {
    constructor(url) {
      this.listeners = {}; this.closed = false;
      const name = new URL(url, location.href).searchParams.get("name") || S.replays[0].name;
      const events = S.events[name] || [];
      const meta = S.replays.find(r => r.name === name) || S.replays[0];
      const speed = 6; let i = 0, last = 0;
      const push = data => this.onmessage && this.onmessage({data: JSON.stringify(data)});
      setTimeout(() => push({type: "replay", ...meta}), 0);
      const step = () => {
        if (this.closed) return;
        if (i >= events.length) { (this.listeners.end || []).forEach(f => f({})); return; }
        const rec = events[i++];
        push(rec.event);
        const next = events[i];
        const gap = next ? Math.min((next.t - rec.t) / speed * 1000, 600) : 0;
        gap > 2 ? setTimeout(step, gap) : (i % 25 ? step() : setTimeout(step, 0));
      };
      setTimeout(step, 50);
    }
    addEventListener(type, f) { (this.listeners[type] ||= []).push(f); }
    close() { this.closed = true; }
  }
  window.EventSource = ReplaySource;
})();
</script>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("recording", nargs="?")
    ap.add_argument("--out", default=str(ROOT / "docs" / "demo" / "index.html"))
    args = ap.parse_args(argv)

    from web import server

    if args.recording:
        path = Path(args.recording)
    else:
        metas = server.replays()
        if not metas:
            print("no recordings in web/replays -- make one with tapeout --record", file=sys.stderr)
            return 1
        path = server.REPLAYS / f"{metas[0]['name']}.jsonl"
    meta = server._replay_meta(path)
    events = [json.loads(line) for line in path.open()]
    hello = next(e["event"] for e in events if e["event"].get("type") == "hello")
    status = {"online": hello.get("online", False), "models": hello.get("models", {}),
              "tavily": hello.get("tavily", False), "pdk": hello.get("pdk", "sky130"),
              "calibrated": hello.get("calibrated", True)}

    page = (ROOT / "web" / "static" / "index.html").read_text()
    data = json.dumps({"status": status, "replays": [meta], "events": {meta["name"]: events}},
                      separators=(",", ":")).replace("</", "<\\/")
    inject = f"<script>window.__STATIC__ = {data};</script>{PLAYER}"
    marker = "<script>\nconst $ = s =>"
    assert marker in page, "index.html changed; update the injection marker"
    page = page.replace(marker, inject + marker, 1)
    page = page.replace("<title>Tapeout</title>", "<title>Tapeout (replay)</title>", 1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    (out.parent.parent / ".nojekyll").touch()
    print(f"wrote {out} ({out.stat().st_size // 1024} KB) from {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
