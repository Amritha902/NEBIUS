"""The architect: a product idea in, a buildable chip plan out.

Nemotron Ultra does the reasoning a senior designer does at the whiteboard --
what signal comes in, how much gain it needs, what decision the logic makes,
how much battery there is -- and writes the plan as JSON.

It never writes a netlist. The plan names blocks from a fixed menu (an
amplifier described by targets with units, or one of twenty logic cells), and
a deterministic compiler checks every block before anything is simulated. When
the compiler rejects a block, the rejection goes back to the architect and it
repairs its own plan. That loop -- propose, compile, read the error, fix -- is
the same loop a coding agent runs against a test suite, with the spec parser
as the test suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bias import design, logic, specparse

from . import llm

MAX_ANALOG = 2
MAX_DIGITAL = 3
REPAIR_ROUNDS = 2
# Measured on sky130 with the DC-servo testbench: the two-stage op-amp
# reaches ~82dB; leave headroom for the corners the red team will try.
GAIN_CEILING_DB = 78.0

# What the fab can reach on sky130, measured rather than assumed. The
# architect is told this so it designs inside the envelope instead of asking
# for a 200 MHz amplifier on 10 uW.
ENVELOPE = """\
ANALOG (sized automatically against SkyWater 130nm foundry models, VDD 1.8V):
  - single-stage OTA: gain up to ~46 dB; GBW 1-15 MHz at 20-100 uW into 1 pF
  - two-stage Miller op-amp: gain 50-78 dB; GBW 1-10 MHz at 50-300 uW
  - every amplifier is then attacked across process corners (tt/ss/ff/sf/fs),
    supply 1.71-1.89 V and -40..85 C, so leave margin: a spec met only at
    typical conditions will be broken and re-sized.
  - load capacitance 0.5-10 pF. Bigger loads need proportionally more power.
  - phase margin >= 55 deg and an output off the rails are always enforced.
  Write each amplifier spec as ONE sentence with units, exactly like:
    "op-amp with 60dB gain, 5MHz bandwidth, under 200uW, 2pF load"
DIGITAL (static CMOS, proved by transient simulation against a truth table):
  {cells}"""

SYSTEM = """You are the chief architect of a mixed-signal ASIC startup. A \
customer describes a product; you decide what silicon it needs and specify \
blocks the automated fab can build and verify.

Think like an analog designer: what physical signal arrives, its amplitude \
and bandwidth, how much gain brings it to a usable level for a 1.8 V supply, \
what the battery allows, what the logic must decide. Use the research facts \
when they exist and cite them by number.

THE FAB CAN BUILD ONLY THIS:
{envelope}

Reply with ONE JSON object and nothing else:
{{
  "part_number": "TPO-<4 uppercase letters or digits>",
  "name": "short product-style chip name",
  "tagline": "one line, what the chip does",
  "signal_chain": "one or two sentences: sensor -> amplifier -> logic -> output",
  "blocks": [
    {{"id": "afe", "kind": "analog", "title": "...", "spec": "op-amp with ...",
      "why": "the reasoning behind each number, citing facts like [1]"}},
    {{"id": "...", "kind": "digital", "title": "...", "cell": "<cell name>",
      "why": "what decision this logic makes in the product"}}
  ]
}}
Use 1-{max_analog} analog blocks and 1-{max_digital} digital blocks. \
Block ids are short snake_case and unique."""


@dataclass
class Block:
    id: str
    kind: str
    title: str
    why: str = ""
    spec: str = ""          # analog: the sentence the compiler parses
    cell: str = ""          # digital: a logic cell name
    targets: list[dict] = field(default_factory=list)
    topology: str = ""
    topology_why: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Plan:
    part_number: str
    name: str
    tagline: str
    signal_chain: str
    blocks: list[Block]
    repairs: list[dict] = field(default_factory=list)
    by_model: bool = True
    fallback: str = ""       # why the reference plan stands in, when it does

    def to_dict(self) -> dict:
        return {
            "part_number": self.part_number,
            "name": self.name,
            "tagline": self.tagline,
            "signal_chain": self.signal_chain,
            "blocks": [b.to_dict() for b in self.blocks],
            "repairs": self.repairs,
            "by_model": self.by_model,
            "fallback": self.fallback,
        }


def envelope() -> str:
    cells = []
    for name in sorted(logic.CELLS):
        _, ins, outs, _ = logic.get(name)
        cells.append(f"{name}({','.join(ins)} -> {','.join(outs)})")
    return ENVELOPE.format(cells=", ".join(cells))


# ---------------------------------------------------------------------------
# The compiler: deterministic, and the architect's test suite.
# ---------------------------------------------------------------------------


def compile_plan(raw: dict) -> tuple[Plan | None, list[str]]:
    """Check a plan the model wrote. Returns (plan, errors)."""
    errors: list[str] = []
    blocks: list[Block] = []
    seen: set[str] = set()

    raw_blocks = raw.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        return None, ["'blocks' must be a non-empty list"]

    for i, rb in enumerate(raw_blocks):
        if not isinstance(rb, dict):
            errors.append(f"block {i} is not an object")
            continue
        bid = re.sub(r"[^a-z0-9_]", "_", str(rb.get("id") or f"b{i}").lower())[:24]
        if bid in seen:
            bid = f"{bid}_{i}"
        seen.add(bid)
        kind = str(rb.get("kind", "")).lower()
        b = Block(bid, kind, str(rb.get("title") or bid), str(rb.get("why", "")))

        if kind == "analog":
            b.spec = str(rb.get("spec", "")).strip()
            try:
                parsed = specparse.parse(b.spec)
            except ValueError as exc:
                errors.append(f"block '{bid}': spec {b.spec!r} has no targets "
                              f"with units ({str(exc).splitlines()[0]})")
                continue
            gain = parsed.found.get("gain")
            if gain is None:
                errors.append(f"block '{bid}': spec must state a gain in dB")
                continue
            if gain > GAIN_CEILING_DB:
                errors.append(f"block '{bid}': {gain:.0f}dB gain is beyond the "
                              f"fab's two-stage op-amp on sky130 (max ~{GAIN_CEILING_DB:.0f}dB); "
                              "lower it or split the gain across two amplifier blocks")
                continue
            gbw = parsed.found.get("gbw")
            if gbw is not None and gbw > 25e6:
                errors.append(f"block '{bid}': {gbw / 1e6:.0f}MHz bandwidth is "
                              "beyond the fab's envelope (max ~20MHz)")
                continue
            if parsed.cl is not None and not (0.1e-12 <= parsed.cl <= 20e-12):
                errors.append(f"block '{bid}': load must be 0.1-20pF")
                continue
            if parsed.unmatched:
                errors.append(f"block '{bid}': could not place {parsed.unmatched} "
                              "in spec -- use gain (dB), bandwidth (Hz), power (W), "
                              "load (F) only")
                continue
            b.topology, b.topology_why = design.choose_topology(gain)
            b.targets = [
                {"name": m.name, "relation": ">=" if m.direction == "max" else "<=",
                 "target": m.target, "unit": m.unit,
                 "implied": m.name in parsed.implied}
                for m in parsed.spec.metrics
            ]
            if parsed.cl:
                b.targets.append({"name": "load", "relation": "=", "target": parsed.cl,
                                  "unit": "F", "implied": False})
        elif kind == "digital":
            cell = str(rb.get("cell", "")).strip().lower()
            if cell not in logic.CELLS:
                errors.append(f"block '{bid}': unknown cell {cell!r}; choose one "
                              f"of {', '.join(sorted(logic.CELLS))}")
                continue
            b.cell = cell
        else:
            errors.append(f"block '{bid}': kind must be 'analog' or 'digital'")
            continue
        blocks.append(b)

    n_a = sum(b.kind == "analog" for b in blocks)
    n_d = sum(b.kind == "digital" for b in blocks)
    if n_a > MAX_ANALOG:
        errors.append(f"at most {MAX_ANALOG} analog blocks (got {n_a})")
    if n_d > MAX_DIGITAL:
        errors.append(f"at most {MAX_DIGITAL} digital blocks (got {n_d})")
    if not errors and n_a == 0:
        errors.append("the plan needs at least one analog block")

    if errors:
        return None, errors

    pn = str(raw.get("part_number") or "TPO-0001").upper()
    if not re.fullmatch(r"TPO-[A-Z0-9]{2,6}", pn):
        pn = "TPO-" + re.sub(r"[^A-Z0-9]", "", pn)[-4:].rjust(4, "0")
    return Plan(
        pn,
        str(raw.get("name") or "Untitled ASIC")[:60],
        str(raw.get("tagline") or "")[:160],
        str(raw.get("signal_chain") or "")[:400],
        blocks,
    ), []


# ---------------------------------------------------------------------------


def design_chip(idea: str, research_brief: str, *, ledger: llm.Ledger, emit) -> Plan:
    if not llm.online():
        emit({"type": "log", "agent": "architect",
              "msg": "offline mode: no NEBIUS_API_KEY, using the built-in reference plan"})
        return offline_plan(idea, "no NEBIUS_API_KEY")

    system = SYSTEM.format(envelope=envelope(), max_analog=MAX_ANALOG,
                           max_digital=MAX_DIGITAL)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"PRODUCT IDEA: {idea}\n\nRESEARCH FACTS:\n{research_brief}"},
    ]
    repairs: list[dict] = []

    for round_ in range(REPAIR_ROUNDS + 1):
        tier = "ultra" if round_ == 0 else "super"
        answer, raw = "", None
        try:
            answer, _ = llm.chat(
                tier, messages, ledger=ledger, max_tokens=6000, temperature=0.5,
                on_delta=lambda k, t: emit({"type": "token", "agent": "architect",
                                            "kind": k, "text": t}),
            )
            raw = llm.extract_json(answer)
        except llm.LLMError as exc:
            if tier != "ultra":
                raise
            # Ultra unavailable on this key or overloaded: Super takes over.
            emit({"type": "log", "agent": "architect",
                  "msg": f"Ultra unavailable ({str(exc)[:120]}); Super takes the plan"})
            answer, _ = llm.chat(
                "super", messages, ledger=ledger, max_tokens=6000, temperature=0.5,
                on_delta=lambda k, t: emit({"type": "token", "agent": "architect",
                                            "kind": k, "text": t}),
            )
            try:
                raw = llm.extract_json(answer)
            except ValueError as exc2:
                raw, errors = None, [f"reply was not a JSON object: {exc2}"]
        except ValueError as exc:
            errors = [f"reply was not a JSON object: {exc}"]
        if raw is not None:
            plan, errors = compile_plan(raw)
            if plan:
                plan.repairs = repairs
                return plan

        repairs.append({"round": round_ + 1, "errors": errors})
        emit({"type": "repair", "agent": "compiler", "round": round_ + 1, "errors": errors})
        messages += [
            {"role": "assistant", "content": answer or "{}"},
            {"role": "user", "content":
                "The fab's compiler rejected the plan:\n- " + "\n- ".join(errors)
                + "\nFix exactly these problems and reply with the full corrected JSON."},
        ]

    emit({"type": "log", "agent": "architect",
          "msg": "plan still failed to compile after repairs; using the reference plan"})
    plan = offline_plan(idea, f"Nemotron's plan failed to compile after {len(repairs)} round(s)")
    plan.repairs = repairs
    return plan


def offline_plan(idea: str, reason: str = "") -> Plan:
    """A sound reference plan, so the fab still runs end to end without a key."""
    raw = {
        "part_number": "TPO-REF1",
        "name": "Reference Sensor Front-End",
        "tagline": "Low-power amplifier plus decision logic for a battery sensor node",
        "signal_chain": "Sensor -> two-stage amplifier -> threshold comparison -> wake decision",
        "blocks": [
            {"id": "afe", "kind": "analog", "title": "Sensor amplifier",
             "spec": "op-amp with 60dB gain, 2MHz bandwidth, under 200uW, 2pF load",
             "why": "60 dB brings a ~1 mV sensor signal to ~1 V on a 1.8 V supply."},
            {"id": "detect", "kind": "digital", "title": "Level comparator",
             "cell": "comparator1",
             "why": "Compares the digitised level against a stored threshold."},
            {"id": "vote", "kind": "digital", "title": "Debounce voter",
             "cell": "majority3",
             "why": "2-of-3 vote over consecutive samples rejects single glitches."},
        ],
    }
    plan, errors = compile_plan(raw)
    assert plan, errors
    plan.by_model = False
    plan.fallback = reason or "reference plan"
    return plan
