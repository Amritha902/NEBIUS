"""Prove the stack works before you demo it.

    python -m tapeout.check             simulator + Token Factory + Tavily
    python -m tapeout.check --offline   simulator only (no keys needed)

Checks ngspice and the sky130 models with a real corner simulation, then
lists the models your key can see, resolves the three Nemotron tiers, makes
one tiny call to each, and checks Tavily. Every failure says how to fix it.
Exits non-zero if anything a run depends on is missing.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time

from . import llm, research


def line(status: str, what: str, detail: str = "") -> None:
    print(f"  [{status:^4}] {what:<34} {detail}")


def check_simulator() -> bool:
    print("Simulator")
    if not shutil.which("ngspice"):
        line("FAIL", "ngspice", "not found -- apt install ngspice  (macOS: brew install ngspice)")
        return False
    line("ok", "ngspice", shutil.which("ngspice"))

    from bias import pdk, specparse, topology
    if "sky130" not in pdk.available():
        line("FAIL", "sky130 models", "not fetched -- run scripts/fetch_pdk.sh sky130")
        return False
    line("ok", "sky130 models", "tt/ss/ff/sf/fs corners present")

    from .falsify import Condition, Snapshot
    t = topology.get("ota5t")
    snap = Snapshot(t, t.defaults(), pdk.get("sky130"), 1e-12,
                    specparse.parse("op-amp with 30dB gain").spec)
    t0 = time.perf_counter()
    run = snap.simulate(Condition("ss", 1.71, 85.0, 1e-12, 0.5))
    if not run.ok:
        line("FAIL", "corner simulation", "ngspice ran but produced no result:\n" + run.log[-600:])
        return False
    line("ok", "corner simulation (ss/1.71V/85C)",
         f"gain {run.measured['gain']:.1f} dB in {time.perf_counter() - t0:.1f}s")
    return True


def check_models() -> bool:
    print(f"Nebius Token Factory  ({llm.BASE_URL})")
    if not llm.api_key():
        line("FAIL", "NEBIUS_API_KEY", "not set -- export it, or put it in .env")
        return False
    ok = True
    models = llm.discover()
    if llm.AVAILABLE:
        nemos = [i for i in llm.AVAILABLE if "nemotron" in i.lower()]
        line("ok", "/v1/models", f"{len(llm.AVAILABLE)} models, {len(nemos)} Nemotron")
    else:
        line("FAIL", "/v1/models", "no list returned -- is the key valid for this endpoint?")
        ok = False

    for tier in ("ultra", "super", "nano"):
        mid = models[tier]
        print(f"  RESOLVED {tier:<5}: {mid}")
        if llm.AVAILABLE and mid not in llm.AVAILABLE:
            line("FAIL", f"{tier} model", "not in /v1/models; pin one with "
                 f"TAPEOUT_MODEL_{tier.upper()}=<id> from:")
            for i in llm.AVAILABLE:
                print(f"           - {i}")
            ok = False
            continue
        t0 = time.perf_counter()
        try:
            answer, think = llm.chat(
                tier, [{"role": "user", "content":
                        'Reply with the JSON object {"ok": true} and nothing else.'}],
                max_tokens=400, temperature=0, json_mode=True, timeout=120)
            parsed = llm.extract_json(answer).get("ok") is True
            line("ok" if parsed else "WARN", f"{tier} call",
                 f"{time.perf_counter() - t0:.1f}s"
                 + ("" if parsed else f"  reply was not the JSON asked for: {answer[:80]!r}")
                 + (f"  (+{len(think)} chars of reasoning)" if think else ""))
        except (llm.LLMError, ValueError) as exc:
            line("FAIL", f"{tier} call", str(exc)[:200])
            ok = False
    return ok


def check_tavily() -> bool:
    print("Tavily")
    if not research.tavily_key():
        line("WARN", "TAVILY_API_KEY", "not set; the scout will run as a declared fallback")
        return True
    try:
        hits = research.search("photoplethysmography signal bandwidth", max_results=2)
        line("ok" if hits else "WARN", "search", f"{len(hits)} results")
        return True
    except Exception as exc:                                  # report, don't crash
        line("FAIL", "search", str(exc)[:200])
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tapeout.check", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="check only the simulator path (no API keys needed)")
    args = ap.parse_args(argv)

    ok = check_simulator()
    if args.offline:
        print("\nSIMULATION PATH READY" if ok else "\nSIMULATION PATH NOT READY -- fix the FAIL lines")
        return 0 if ok else 1
    ok = check_models() and ok
    ok = check_tavily() and ok
    print("\nLIVE PATH READY" if ok else "\nLIVE PATH NOT READY -- fix the FAIL lines above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
