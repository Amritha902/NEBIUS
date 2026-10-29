"""Command line: run a tape-out and print what each agent did.

    tapeout "a heart-rate monitor front-end for a fitness band" --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys

from . import pipeline


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tapeout", description=__doc__)
    ap.add_argument("idea", help="the product, in plain words")
    ap.add_argument("--json", help="write the full result (datasheet, falsification log) here")
    ap.add_argument("--quiet", action="store_true", help="only print the verdict")
    args = ap.parse_args(argv)

    def emit(e: dict) -> None:
        if args.quiet:
            return
        t = e["type"]
        if t == "stage":
            print(f"[{e['agent']:>9}] {e['status']}", flush=True)
        elif t in ("log",):
            print(f"[{e['agent']:>9}] {e['msg']}", flush=True)
        elif t == "finding":
            print(f"[  redteam] round {e['round']} {e['hypothesis']['id']} "
                  f"{e['verdict'].upper():<9} {e['statement']}", flush=True)
        elif t == "repair_start":
            print(f"[      fab] repair {e['round']}: must also hold at {'; '.join(e['conditions'])}",
                  flush=True)
        elif t == "error":
            print(e["trace"], file=sys.stderr)

    result = pipeline.safe_tapeout(args.idea, emit)
    if result is None:
        return 1

    p = result["plan"]
    print(f"\n{p['part_number']} {p['name']} -- {p['tagline']}")
    for c in result["campaigns"]:
        print(f"\nRobustness envelope: {c['title']}  ({c['sims']} stress sims, "
              f"{len(c['rounds'])} round(s), {len(c['repairs'])} repair(s))")
        for row in c["robustness"]:
            print(f"  {row['claim']:<5} {row['relation']} {row['target']:<10.4g} "
                  f"{row['status'].upper():<9} worst {row['worst'] if row['worst'] is not None else 'n/a':<10.4g} "
                  f"at {row['worst_condition']}")
    print(f"\n{result['simulations']} simulations, {result['seconds']}s, "
          f"{'robust' if result['robust'] else 'DERATED'}")

    if args.json:
        def clean(x):
            if isinstance(x, float):
                return x if math.isfinite(x) else None
            if isinstance(x, dict):
                return {k: clean(v) for k, v in x.items()}
            if isinstance(x, list):
                return [clean(v) for v in x]
            return x
        with open(args.json, "w") as f:
            json.dump(clean(result), f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
