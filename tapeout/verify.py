"""Re-run the evidence: re-simulate every recorded deck and check the numbers.

    python -m tapeout.verify web/replays/ppg-offline.jsonl
    python -m tapeout.verify run.json            # a `tapeout --json` result

A datasheet that says "every number is traceable" should let anyone check.
This takes a recorded run, re-runs every SPICE deck it kept (every hypothesis
probe, worst case and counterexample) on *your* ngspice and *your* copy of
the sky130 models, and compares each measurement with what the run recorded.
It trusts nothing from the run except the deck text itself.

Exit status is 0 only if every deck re-simulates to the recorded values.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bias.pdk import PDK_DIR
from bias.sim import run_deck
from bias.topology import phase_margin

# Measurements compared, and how they come out of the deck.
CHECKED = ("gain", "gbw", "pwr", "vout_dc", "pm")
REL_TOL = 1e-3        # ngspice prints ~7 significant digits; this is generous


def load(path: Path) -> dict:
    """The final result of a run, from a replay recording or a --json file."""
    text = path.read_text()
    if path.suffix == ".jsonl":
        for line in text.splitlines():
            event = json.loads(line)["event"]
            if event.get("type") == "done":
                return event
        raise ValueError(f"{path} has no finished run in it")
    return json.loads(text)


def localise(deck: str) -> str:
    """Point the deck's model includes at this machine's PDK checkout."""
    return re.sub(r"(\.include\s+)\S*?/pdks/", lambda m: m.group(1) + str(PDK_DIR) + "/", deck)


def _close(a, b) -> bool:
    # Recordings are JSON, which has no NaN: an undefined measurement (no
    # unity-gain crossing, so no phase margin) was stored as null.
    a = None if a is None or not math.isfinite(a) else a
    b = None if b is None or not math.isfinite(b) else b
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1e-12)


def check(run: dict) -> dict:
    res = run_deck(localise(run["deck"]))
    got = dict(res.values)
    got["pm"] = phase_margin(got.get("ph_dc", float("nan")), got.get("ph_fu", float("nan")))
    rows = []
    for k in CHECKED:
        want = run["measured"].get(k)
        if want is None and k not in got:
            continue
        have = got.get(k)
        rows.append({"metric": k, "recorded": want, "resimulated": have,
                     "match": _close(want, have)})
    return {"id": run["id"], "condition": run["condition"], "ok": res.ok,
            "rows": rows, "match": res.ok and all(r["match"] for r in rows)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tapeout.verify", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", type=Path, help="a web/replays/*.jsonl or a tapeout --json file")
    ap.add_argument("--report", type=Path, help="also write the per-run comparison as JSON")
    args = ap.parse_args(argv)

    result = load(args.recording)
    runs = [r for c in result.get("campaigns", []) for r in c["runs"].values() if r.get("deck")]
    if not runs:
        print("no stored decks in this recording")
        return 1
    print(f"Re-simulating {len(runs)} recorded decks from {args.recording.name} "
          f"on this machine's ngspice and {PDK_DIR} ...")
    with ThreadPoolExecutor(max_workers=4) as pool:
        checks = list(pool.map(check, runs))

    bad = [c for c in checks if not c["match"]]
    values = sum(len(c["rows"]) for c in checks)
    for c in bad[:20]:
        diffs = ", ".join(f"{r['metric']} recorded {r['recorded']} got {r['resimulated']}"
                          for r in c["rows"] if not r["match"])
        print(f"  MISMATCH run #{c['id']} ({c['condition']}): {diffs or 'simulation failed'}")
    if args.report:
        args.report.write_text(json.dumps(checks, indent=1))
    if bad:
        print(f"\n{len(bad)} of {len(checks)} runs did NOT reproduce.")
        return 1
    print(f"\nVERIFIED: all {len(checks)} runs reproduced; {values} recorded "
          f"measurements matched within {REL_TOL:.0e} relative.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
