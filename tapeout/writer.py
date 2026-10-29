"""The datasheet writer, and the auditor that checks every number it writes.

Nemotron Nano writes the prose a datasheet needs -- overview, features,
applications. Language models are fluent with numbers they do not have, and a
datasheet with one invented figure is worse than no datasheet. So the writer
is audited: every quantity with a unit in its prose, and every transistor
count, is matched against what the simulator measured or what the spec
demanded. A number with no source goes back to the writer once; anything that
survives that is struck through on the page, not quietly published.
"""

from __future__ import annotations

import math
import re

from bias import specparse

from . import llm

TOLERANCE = 0.05

PROMPT = """Write the front page of a datasheet for a chip that was just \
designed and verified in simulation.

The chip was also attacked by a red team that tried to break each claim \
across process corners, supply and temperature. Where a claim holds across \
the envelope, say so; where the red team found a limit, state the limit \
plainly -- an honest limit is a feature of this datasheet, not a weakness.

Use ONLY the numbers in the FACTS below. Round sensibly (61.27dB -> 61.3dB) \
but never introduce a number that is not there. Do not invent package, \
temperature range, noise, slew rate or any figure not listed. Write in the \
confident, compact voice of a real analog datasheet.

FACTS:
{facts}

Reply with ONE JSON object:
{{"overview": "3-4 sentences",
  "features": ["5-7 short bullet strings, each leading with a number from FACTS"],
  "applications": ["3-4 short strings"]}}"""


def facts_sheet(plan, analog, digital, pdk, campaigns=()) -> tuple[str, dict[str, list[float]]]:
    """The facts given to the writer, and the numbers the auditor accepts."""
    known: dict[str, list[float]] = {"V": [pdk.vdd], "count": []}
    lines = [
        f"Chip: {plan.part_number} {plan.name} -- {plan.tagline}",
        f"Signal chain: {plan.signal_chain}",
        f"Process: {pdk.name}, supply {pdk.vdd:g}V",
    ]
    total_t = 0
    for a in analog:
        lines.append(f"Block {a.block.id} ({a.block.title}): {a.topology} amplifier, "
                     f"{'meets all targets' if a.solved else 'DOES NOT meet all targets'}")
        for t in a.targets:
            unit = t["unit"]
            known.setdefault(unit or "", []).append(t["target"])
            meas = t.get("measured")
            if meas is not None:
                known.setdefault(unit or "", []).append(meas)
            if t["name"] == "load":
                lines.append(f"  load capacitance: {t['target'] * 1e12:.3g}pF")
                continue
            lines.append(
                f"  {t['name']}: target {t['relation']} {_fmt(t['target'], t['unit'])}, "
                f"measured {_fmt(meas, t['unit']) if meas is not None else 'n/a'}")
        tc = 6 if a.topology == "ota5t" else 8
        total_t += tc
        known["count"].append(tc)
    for d in digital:
        lines.append(f"Block {d.block.id} ({d.block.title}): {d.block.cell}, "
                     f"{d.transistors} transistors, truth table "
                     f"{'proved' if d.passed else 'FAILED'} over {len(d.rows)} input combinations")
        known["count"] += [d.transistors, len(d.rows)]
        total_t += d.transistors
    for c in campaigns:
        lines.append(f"Red-team campaign on {c['block']}: {len(c['hypotheses'])} failure "
                     f"hypotheses, {c['sims']} stress simulations, "
                     f"{len(c['repairs'])} repair round(s)")
        for row in c["robustness"]:
            worst = row.get("worst")
            if worst is not None:
                known.setdefault(row["unit"], []).append(worst)
            lines.append(
                f"  {row['claim']}: {row['status'].upper()} across the operating envelope "
                f"(all corners, 1.71-1.89V, -40 to 85C); worst case "
                f"{_fmt(worst, row['unit'])} at {row['worst_condition']}")
            for lim in row["limits"]:
                lines.append(f"    limit found: {lim}")
        for rd in c["rounds"]:
            for f in rd["findings"]:
                if f.get("boundary") and f["boundary"]["axis"] == "cl_x":
                    for k in ("holds_to", "breaks_at"):
                        known.setdefault("F", []).append(f["boundary"][k] * c["nominal_cl"])
        known["count"] += [c["sims"], len(c["hypotheses"])]
    known["count"].append(total_t)
    lines.append(f"Total transistors: {total_t}")
    return "\n".join(lines), known


def _fmt(v, unit: str) -> str:
    if v is None or not math.isfinite(v):
        return "n/a"
    if unit in ("dB", "deg"):
        return f"{v:.1f}{unit}"
    if unit == "":
        return f"{v:.3g}"
    from bias.spec import _eng
    return f"{_eng(v)}{unit}"


COUNT = re.compile(r"\b(\d+)[\s-]*(?:transistors?|mosfets?|devices|input combinations|combinations)\b", re.I)


def audit_text(text: str, known: dict[str, list[float]]) -> list[str]:
    """Quantities in `text` that no fact supports."""
    bad = []
    for q in specparse.quantities(text):
        pool = known.get(q.unit, [])
        if q.unit == "dB" and not pool:
            pool = known.get("dB", [])
        if not any(_close(q.value, k) for k in pool):
            bad.append(q.text.strip())
    for m in COUNT.finditer(text):
        n = int(m.group(1))
        if n not in known.get("count", []):
            bad.append(m.group(0))
    return bad


def _close(a: float, b: float) -> bool:
    if b == 0:
        return abs(a) < 1e-12
    return abs(a - b) / abs(b) <= TOLERANCE


def audit(sheet: dict, known) -> list[dict]:
    flagged = []
    for field_name in ("overview",):
        for claim in audit_text(sheet.get(field_name, ""), known):
            flagged.append({"field": field_name, "claim": claim})
    for field_name in ("features", "applications"):
        for i, s in enumerate(sheet.get(field_name, [])):
            for claim in audit_text(s, known):
                flagged.append({"field": f"{field_name}[{i}]", "claim": claim})
    return flagged


def _clean(raw: dict) -> dict:
    return {
        "overview": str(raw.get("overview", ""))[:1200],
        "features": [str(x)[:200] for x in raw.get("features", []) if x][:8],
        "applications": [str(x)[:120] for x in raw.get("applications", []) if x][:5],
    }


def write(plan, analog, digital, pdk, *, ledger, emit, campaigns=()) -> dict:
    facts, known = facts_sheet(plan, analog, digital, pdk, campaigns)

    if not llm.online():
        sheet = offline_sheet(plan, analog, digital, pdk)
        return {"sheet": sheet, "facts": facts, "flagged": audit(sheet, known),
                "rewrites": 0, "by_model": False}

    messages = [{"role": "user", "content": PROMPT.format(facts=facts)}]
    sheet, flagged, rewrites = None, [], 0
    for round_ in range(2):
        try:
            answer, _ = llm.chat(
                "nano", messages, ledger=ledger, json_mode=True, reasoning=False,
                max_tokens=1200, temperature=0.4,
                on_delta=lambda k, t: emit({"type": "token", "agent": "writer",
                                            "kind": k, "text": t}),
            )
            sheet = _clean(llm.extract_json(answer))
        except (llm.LLMError, ValueError) as exc:
            emit({"type": "log", "agent": "writer", "msg": f"writer failed: {exc}"})
            break
        flagged = audit(sheet, known)
        emit({"type": "audit", "round": round_ + 1, "flagged": flagged})
        if not flagged:
            break
        rewrites += 1
        messages += [
            {"role": "assistant", "content": answer},
            {"role": "user", "content":
                "These figures are not in FACTS and must not appear: "
                + ", ".join(f["claim"] for f in flagged)
                + ". Rewrite the full JSON using only numbers from FACTS."},
        ]

    if sheet is None:
        sheet = offline_sheet(plan, analog, digital, pdk)
        flagged = audit(sheet, known)
    return {"sheet": sheet, "facts": facts, "flagged": flagged,
            "rewrites": rewrites, "by_model": True}


def offline_sheet(plan, analog, digital, pdk) -> dict:
    feats = []
    for a in analog:
        for t in a.targets:
            if t["name"] in ("gain", "gbw", "pwr", "pm") and t.get("measured") is not None:
                feats.append(f"{_fmt(t['measured'], t['unit'])} {t['name']} ({a.block.title})")
    for d in digital:
        feats.append(f"{d.transistors} transistors: {d.block.title} ({d.block.cell})")
    return {
        "overview": f"{plan.name} is a mixed-signal block set on {pdk.name}. "
                    f"{plan.signal_chain}",
        "features": feats[:7],
        "applications": ["Battery-powered sensor nodes", "Wake-on-event front ends"],
    }
