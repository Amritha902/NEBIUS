"""HTTP front end: one click from a product idea to a verified chip.

GET /api/tapeout?idea=... streams the whole run as server-sent events -- every
token an agent thinks, every simulation the fab spends, every truth-table row
the simulator proves -- and ends with the finished datasheet.

Run locally:   uvicorn web.server:app --port 8010
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from tapeout import llm, pipeline, research

STATIC = Path(__file__).parent / "static"
REPLAYS = Path(__file__).parent / "replays"
MAX_CONCURRENT = int(os.environ.get("TAPEOUT_MAX_CONCURRENT", "3"))

app = FastAPI(title="Tapeout", docs_url="/api/docs")
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


def _clean(x):
    """JSON without NaN/Infinity, which browsers refuse to parse."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


@app.get("/api/status")
def status() -> dict:
    p = pipeline.default_pdk()
    return {
        "online": llm.online(),
        "models": llm.discover(),
        "tavily": bool(research.tavily_key()),
        "pdk": p.name,
        "calibrated": p.calibrated,
    }


@app.get("/api/tapeout")
async def tapeout(idea: str = Query(min_length=3, max_length=500)):
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, "the fab is busy -- try again in a minute")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def work() -> None:
        try:
            pipeline.safe_tapeout(idea.strip(), emit)
        finally:
            _slots.release()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=work, daemon=True).start()

    async def stream():
        yield "retry: 600000\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"           # proxies drop idle streams
                continue
            if event is None:
                yield "event: end\ndata: {}\n\n"
                return
            yield f"data: {json.dumps(_clean(event))}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _replay_meta(path: Path) -> dict | None:
    """Name, idea and mode of a recorded run, read from its first events."""
    try:
        with path.open() as f:
            for line in f:
                e = json.loads(line)["event"]
                if e.get("type") == "hello":
                    return {"name": path.stem, "idea": e.get("idea", ""),
                            "online": bool(e.get("online")),
                            "recorded": path.stat().st_mtime}
    except (OSError, ValueError, KeyError):
        return None
    return None


@app.get("/api/replays")
def replays() -> list[dict]:
    """Recorded runs, live ones first."""
    metas = [m for p in sorted(REPLAYS.glob("*.jsonl")) if (m := _replay_meta(p))]
    return sorted(metas, key=lambda m: (not m["online"], -m["recorded"]))


@app.get("/api/replay")
async def replay(name: str = Query(pattern=r"^[A-Za-z0-9_.-]+$"),
                 speed: float = Query(default=6.0, ge=1.0, le=60.0)):
    """Stream a recorded run, compressed in time, labelled as a replay.

    Every event is exactly what the recorded run emitted -- the same decks,
    logs and measurements -- so a viewer can audit it without waiting minutes
    for fresh simulations or spending model credits.
    """
    path = REPLAYS / f"{name}.jsonl"
    meta = _replay_meta(path)
    if not meta:
        raise HTTPException(404, "no such recording")

    async def stream():
        yield "retry: 600000\n\n"
        yield f"data: {json.dumps({'type': 'replay', **meta})}\n\n"
        last = 0.0
        with path.open() as f:
            for line in f:
                rec = json.loads(line)
                gap = (rec["t"] - last) / speed
                last = rec["t"]
                if gap > 0.002:
                    await asyncio.sleep(min(gap, 0.6))
                yield f"data: {json.dumps(rec['event'])}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
