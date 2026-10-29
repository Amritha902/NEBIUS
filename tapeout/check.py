"""Prove the live path works before you demo it.

    python -m tapeout.check

Lists the models this key can see, resolves the three Nemotron tiers, makes
one tiny call to each, checks Tavily, and exits non-zero if anything a live
run depends on is missing. Takes ~30 seconds.
"""

from __future__ import annotations

import sys
import time

from . import llm, research


def main() -> int:
    ok = True

    def line(status: str, what: str, detail: str = "") -> None:
        print(f"  [{status:^4}] {what:<34} {detail}")

    print(f"Token Factory: {llm.BASE_URL}")
    if not llm.api_key():
        line("FAIL", "NEBIUS_API_KEY", "not set")
        return 1
    models = llm.discover()
    nemos = [i for i in llm.AVAILABLE if "nemotron" in i.lower()]
    if llm.AVAILABLE:
        line("ok", "/v1/models", f"{len(llm.AVAILABLE)} models, {len(nemos)} Nemotron")
        for i in nemos:
            print(f"           - {i}")
    else:
        line("FAIL", "/v1/models", "no list returned (bad key or endpoint?)")
        ok = False

    for tier in ("ultra", "super", "nano"):
        mid = models[tier]
        listed = "" if mid in llm.AVAILABLE else "  (NOT in /v1/models)"
        t0 = time.perf_counter()
        try:
            answer, think = llm.chat(
                tier, [{"role": "user", "content":
                        'Reply with the JSON object {"ok": true} and nothing else.'}],
                max_tokens=400, temperature=0, json_mode=True, timeout=120)
            parsed = llm.extract_json(answer).get("ok") is True
            line("ok" if parsed else "WARN", f"{tier}: {mid}",
                 f"{time.perf_counter() - t0:.1f}s"
                 + ("" if parsed else f"  reply was not the JSON asked for: {answer[:80]!r}")
                 + (f"  (+{len(think)} chars of reasoning)" if think else "") + listed)
        except (llm.LLMError, ValueError) as exc:
            line("FAIL", f"{tier}: {mid}", str(exc)[:160] + listed)
            ok = False

    if research.tavily_key():
        try:
            hits = research.search("photoplethysmography signal bandwidth", max_results=2)
            line("ok" if hits else "WARN", "Tavily search", f"{len(hits)} results")
        except Exception as exc:                              # report, don't crash
            line("FAIL", "Tavily search", str(exc)[:160])
            ok = False
    else:
        line("WARN", "TAVILY_API_KEY", "not set; the scout will be a declared fallback")

    print("\nLIVE PATH READY" if ok else "\nLIVE PATH NOT READY -- fix the FAIL lines above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
