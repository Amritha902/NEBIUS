"""The red team: try to break the datasheet, and publish what broke.

A sized amplifier comes out of the fab meeting its spec at one point: typical
process, 1.8 V, 27 C, the load it was asked for. A datasheet that stops there
is a claim about one simulation. This module attacks every claim:

  stress proposer   Nemotron reads the topology, the sizing and how much
                    margin each claim has, and proposes the conditions most
                    likely to break it -- with the mechanism it expects.
  boundary searcher deterministic. Tests the hypothesis at its extreme, and
                    if the claim breaks, bisects back toward nominal to find
                    the exact boundary.
  corner grid       the brute-force baseline: every process corner x supply
                    x temperature inside the operating envelope. It keeps the
                    adversary honest -- if the grid finds a violation the
                    adversary missed, the report says so.
  recorder          every run is a counterexample record: the condition, the
                    measured value, the exact deck, the ngspice log, and a
                    hash of the design it ran against.

The nominal design is never mutated. Each stress run is a fork: the frozen
sizing plus a condition override, rendered into its own deck and simulated in
its own ngspice process, so a failed run is a leaf in the log rather than a
change to the design.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from bias.pdk import PDK
from bias.sim import run_deck
from bias.spec import Spec
from bias.topology import Testbench, Topology

from . import llm

CORNERS = ("tt", "ss", "ff", "sf", "fs")
CORNER_NAMES = {
    "tt": "typical", "ss": "slow-slow", "ff": "fast-fast",
    "sf": "slow-n/fast-p", "fs": "fast-n/slow-p",
}

# What the datasheet promises: the operating envelope. A claim that breaks
# inside it is a bug in the design; one that breaks outside it is a limit to
# publish.
ENVELOPE = {"vdd": (1.71, 1.89), "temp": (-40.0, 85.0), "vcm": (0.4, 0.6)}
# Where the adversary may push to find absolute limits.
PROBE = {"vdd": (1.5, 2.0), "temp": (-55.0, 150.0), "vcm": (0.1, 0.9),
         "cl_x": (1.0, 10.0)}
BISECT_STEPS = 5
MAX_HYPOTHESES = 6
CLAIMS = ("gain", "gbw", "pm", "pwr")


@dataclass(frozen=True)
class Condition:
    corner: str = "tt"
    vdd: float = 1.8
    temp: float = 27.0
    cl: float = 1e-12
    vcm: float = 0.5

    def label(self, nominal_cl: float) -> str:
        parts = [self.corner, f"{self.vdd:.3g}V", f"{self.temp:.0f}C"]
        if abs(self.cl - nominal_cl) > 1e-18:
            parts.append(f"CL={self.cl * 1e12:.3g}pF")
        if abs(self.vcm - 0.5) > 1e-9:
            parts.append(f"Vcm={self.vcm:.2f}*VDD")
        return " / ".join(parts)

    def in_envelope(self, nominal_cl: float) -> bool:
        return (ENVELOPE["vdd"][0] - 1e-9 <= self.vdd <= ENVELOPE["vdd"][1] + 1e-9
                and ENVELOPE["temp"][0] <= self.temp <= ENVELOPE["temp"][1]
                and ENVELOPE["vcm"][0] - 1e-9 <= self.vcm <= ENVELOPE["vcm"][1] + 1e-9
                and self.cl <= nominal_cl * 1.0001)


@dataclass
class Run:
    """One fork: frozen design + one condition, simulated."""

    id: str
    condition: Condition
    measured: dict[str, float]
    ok: bool
    deck: str
    log: str
    design_hash: str


@dataclass
class Snapshot:
    """The immutable design every fork starts from."""

    topology: Topology
    values: dict[str, float]
    pdk: PDK
    cl: float
    spec: Spec
    runs: dict[Condition, Run] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def nominal(self) -> Condition:
        return Condition(vdd=self.pdk.vdd, cl=self.cl)

    @property
    def hash(self) -> str:
        blob = self.topology.name + repr(sorted(self.values.items()))
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def simulate(self, cond: Condition) -> Run:
        with self.lock:
            if cond in self.runs:
                return self.runs[cond]
        pdk = dataclasses.replace(
            self.pdk, vdd=cond.vdd,
            include=self.pdk.include.replace("__tt.corner", f"__{cond.corner}.corner"),
        )
        tb = Testbench(cl=cond.cl, temp=cond.temp, vcm_frac=cond.vcm, servo=True)
        deck = self.topology.deck(pdk, self.values, tb)
        res = run_deck(deck)
        measured = self.topology.postprocess(res.values, self.values, pdk)
        run = Run(
            id=hashlib.sha256((self.hash + repr(cond)).encode()).hexdigest()[:8],
            condition=cond,
            measured={k: float(v) for k, v in measured.items()},
            ok=res.ok,
            deck=deck,
            log=(res.stdout or "")[-2500:] + ("\n" + res.stderr[-800:] if res.stderr else ""),
            design_hash=self.hash,
        )
        with self.lock:
            self.runs[cond] = run
        return run

    def holds(self, claim: str, run: Run) -> bool:
        m = next(m for m in self.spec.metrics if m.name == claim)
        return run.ok and m.satisfied(run.measured.get(claim, float("nan")))


def claims_of(spec: Spec) -> list:
    return [m for m in spec.metrics if m.name in CLAIMS]


# ---------------------------------------------------------------------------
# Hypotheses
# ---------------------------------------------------------------------------


@dataclass
class Hypothesis:
    id: str
    claim: str
    mechanism: str
    fixed: dict                    # corner / vdd / temp / vcm / cl_x overrides
    axis: str                      # vdd | temp | vcm | cl_x | none
    toward: float | None = None
    by: str = "nemotron"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


PROPOSER_SYSTEM = """You are the red-team engineer on an analog IC project. \
A circuit was just sized to meet its datasheet at ONE point: typical process, \
1.8 V supply, 27 C, nominal load. Your job is to break the datasheet: find \
the operating conditions under which a claimed number stops being true.

Reason from the circuit, not from a grid. Which device leaves saturation when \
the supply droops? Which pole moves when the load grows? Which corner makes \
the second stage faster than the compensation assumed? Claims with thin \
margin are the best targets.

You may set these stress knobs:
  corner: one of tt, ss, ff, sf, fs  (sf = slow NMOS / fast PMOS)
  vdd:    1.5 .. 2.0 V
  temp:   -55 .. 150 C
  vcm:    input common mode as a fraction of VDD, 0.1 .. 0.9
  cl_x:   load capacitance as a multiple of nominal, 1 .. 10
The promised operating envelope is: all corners, vdd 1.71-1.89, temp -40..85, \
vcm 0.4-0.6, nominal load. Breaking a claim inside the envelope is a design \
bug; finding where it breaks outside is a published limit. Both are valuable.

Each hypothesis fixes some knobs and names ONE axis to push ("toward" its \
extreme); the lab will bisect that axis to find the exact boundary. Use \
axis "none" to test a fixed point only.

Reply with ONE JSON object:
{"hypotheses": [
  {"claim": "pm|gain|gbw|pwr", "mechanism": "one sentence of circuit reasoning",
   "fixed": {"corner": "ff", "temp": 85}, "axis": "cl_x", "toward": 4}
]}
Give 4 to 6 hypotheses, spread across at least three claims."""


def _problem(snap: Snapshot, nominal: Run) -> str:
    lines = [f"TOPOLOGY: {snap.topology.name} -- {snap.topology.description}",
             f"NOMINAL LOAD: {snap.cl * 1e12:.3g} pF", "SIZING:"]
    for p in snap.topology.params:
        lines.append(f"  {p.name} = {snap.values[p.name]:.4g} {p.unit}  ({p.description})")
    lines.append("CLAIMS (target, measured at nominal, margin):")
    for m in claims_of(snap.spec):
        v = nominal.measured.get(m.name, float("nan"))
        rel = ">=" if m.direction == "max" else "<="
        lines.append(f"  {m.name} {rel} {_fmt(m.target, m.unit)}   measured {_fmt(v, m.unit)}"
                     f"   margin {_margin(m, v)}")
    return "\n".join(lines)


def _margin(m, v) -> str:
    if not math.isfinite(v):
        return "n/a"
    if m.unit in ("dB", "deg"):
        d = v - m.target if m.direction == "max" else m.target - v
        return f"{d:+.1f}{m.unit}"
    r = v / m.target if m.direction == "max" else m.target / v
    return f"{(r - 1) * 100:+.0f}%"


def _fmt(v: float, unit: str) -> str:
    if v is None or not math.isfinite(v):
        return "n/a"
    if unit in ("dB", "deg"):
        return f"{v:.1f}{unit}"
    from bias.spec import _eng
    return f"{_eng(v)}{unit}"


def _clean_hypotheses(raw: list, claims: set[str], by: str) -> list[Hypothesis]:
    out: list[Hypothesis] = []
    for i, h in enumerate(raw):
        if not isinstance(h, dict):
            continue
        claim = str(h.get("claim", "")).lower()
        if claim not in claims:
            continue
        fixed = {}
        for k, v in (h.get("fixed") or {}).items():
            k = str(k).lower()
            if k == "corner" and str(v).lower() in CORNERS:
                fixed["corner"] = str(v).lower()
            elif k in PROBE:
                try:
                    lo, hi = PROBE[k]
                    fixed[k] = min(hi, max(lo, float(v)))
                except (TypeError, ValueError):
                    pass
        axis = str(h.get("axis", "none")).lower()
        toward = None
        if axis in PROBE:
            try:
                lo, hi = PROBE[axis]
                toward = min(hi, max(lo, float(h.get("toward"))))
            except (TypeError, ValueError):
                axis = "none"
        else:
            axis = "none"
        fixed.pop(axis, None)
        out.append(Hypothesis(f"H{len(out) + 1}", claim,
                              str(h.get("mechanism", ""))[:300], fixed, axis, toward, by))
        if len(out) >= MAX_HYPOTHESES:
            break
    return out


def heuristic_hypotheses(claims: set[str]) -> list[Hypothesis]:
    """Textbook failure mechanisms, for when no model is available."""
    book = [
        ("pm", "A heavier load pulls the output pole down toward the GBW; the "
               "fast corner raises GBW at the same time.",
         {"corner": "ff"}, "cl_x", 6.0),
        ("pm", "Hot silicon shifts the second-stage pole relative to the "
               "compensation zero.", {"corner": "fs"}, "temp", 150.0),
        ("gbw", "Slow devices and a cold, low supply cut gm of the input pair.",
         {"corner": "ss", "temp": -40}, "vdd", 1.5),
        ("gain", "The input pair leaves saturation as the common mode nears a rail.",
         {}, "vcm", 0.9),
        ("pwr", "Fast devices on a high supply draw more bias current.",
         {"corner": "ff", "temp": -40}, "vdd", 2.0),
        ("gain", "Output resistance falls at high temperature.",
         {"corner": "ss"}, "temp", 150.0),
    ]
    out = []
    for claim, mech, fixed, axis, toward in book:
        if claim in claims:
            out.append(Hypothesis(f"H{len(out) + 1}", claim, mech, fixed, axis,
                                  toward, "heuristic"))
    return out


def propose(snap: Snapshot, nominal: Run, *, ledger, emit,
            previous: list[Hypothesis] | None = None, history: str = "") -> list[Hypothesis]:
    """Failure hypotheses for this design.

    With `previous`, the design has just been repaired: the old hypotheses
    are re-run as regressions by the caller, and the model is asked for new
    attacks on the new sizing -- an adversary that adapts to the fix.
    """
    claims = {m.name for m in claims_of(snap.spec)}
    if not llm.online():
        return [] if previous else heuristic_hypotheses(claims)
    user = _problem(snap, nominal)
    if previous:
        user += ("\n\nTHIS DESIGN WAS JUST RE-SIZED to survive earlier attacks.\n"
                 + history
                 + "\nThe hypotheses below will be re-run as regressions. Propose 2 or 3 "
                 "NEW ones that target what the repair changed:\n"
                 + "\n".join(f"  {h.claim}: {h.mechanism} fixed={h.fixed} axis={h.axis}"
                              for h in previous))
    try:
        answer, _ = llm.chat(
            "ultra",
            [{"role": "system", "content": PROPOSER_SYSTEM},
             {"role": "user", "content": user}],
            ledger=ledger, max_tokens=5000, temperature=0.6,
            on_delta=lambda k, t: emit({"type": "token", "agent": "redteam",
                                        "kind": k, "text": t}),
        )
        hyps = _clean_hypotheses(llm.extract_json(answer).get("hypotheses", []),
                                 claims, "nemotron")
    except (llm.LLMError, ValueError) as exc:
        emit({"type": "log", "agent": "redteam", "msg": f"proposer failed ({exc}); using textbook mechanisms"})
        hyps = []
    if previous:
        start = len(previous)
        for i, h in enumerate(hyps[:3]):
            h.id = f"H{start + i + 1}"
        return hyps[:3]
    return hyps or heuristic_hypotheses(claims)


# ---------------------------------------------------------------------------
# Boundary search
# ---------------------------------------------------------------------------


def _apply(base: Condition, knobs: dict, nominal_cl: float) -> Condition:
    c = base
    for k, v in knobs.items():
        if k == "cl_x":
            c = dataclasses.replace(c, cl=nominal_cl * v)
        elif k in ("corner", "vdd", "temp", "vcm"):
            c = dataclasses.replace(c, **{k: v})
    return c


def _axis_value(c: Condition, axis: str, nominal_cl: float) -> float:
    return c.cl / nominal_cl if axis == "cl_x" else getattr(c, axis)


@dataclass
class Finding:
    hypothesis: Hypothesis
    verdict: str                   # survived | broken | boundary
    runs: list[str]                # Run ids, in order
    breaking_run: str | None = None
    boundary: dict | None = None   # {axis, holds_to, breaks_at}
    in_envelope: bool = False
    statement: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["hypothesis"] = self.hypothesis.to_dict()
        return d


def attack(snap: Snapshot, h: Hypothesis, *, emit, round_: int) -> Finding:
    ncl = snap.cl
    m = next(m for m in snap.spec.metrics if m.name == h.claim)
    start = _apply(snap.nominal, h.fixed, ncl)
    ids: list[str] = []

    def probe(cond: Condition) -> tuple[Run, bool]:
        run = snap.simulate(cond)
        ok = snap.holds(h.claim, run)
        ids.append(run.id)
        emit({"type": "stress", "round": round_, "hypothesis": h.id, "claim": h.claim,
              "run": run.id, "condition": cond.label(ncl),
              "in_envelope": cond.in_envelope(ncl),
              "value": run.measured.get(h.claim), "unit": m.unit, "holds": ok})
        return run, ok

    run0, ok0 = probe(start)
    if not ok0:
        return Finding(h, "broken", ids, run0.id, None, start.in_envelope(ncl),
                       f"{h.claim} fails at {start.label(ncl)}: measured "
                       f"{_fmt(run0.measured.get(h.claim, float('nan')), m.unit)}")
    if h.axis == "none" or h.toward is None:
        return Finding(h, "survived", ids, statement=f"{h.claim} holds at {start.label(ncl)}")

    far = _apply(start, {h.axis: h.toward}, ncl)
    run1, ok1 = probe(far)
    if ok1:
        return Finding(h, "survived", ids, statement=(
            f"{h.claim} holds from {start.label(ncl)} all the way to "
            f"{h.axis}={_axis_fmt(h.axis, h.toward)}"))

    lo = _axis_value(start, h.axis, ncl)
    hi = h.toward
    bad_run = run1
    for _ in range(BISECT_STEPS):
        mid = (lo + hi) / 2
        r, ok = probe(_apply(start, {h.axis: mid}, ncl))
        if ok:
            lo = mid
        else:
            hi, bad_run = mid, r
    breaks = _apply(start, {h.axis: hi}, ncl)
    boundary = {"axis": h.axis, "holds_to": lo, "breaks_at": hi}
    return Finding(
        h, "boundary", ids, bad_run.id, boundary, breaks.in_envelope(ncl),
        f"{h.claim} holds to {h.axis}={_axis_fmt(h.axis, lo)} and breaks at "
        f"{_axis_fmt(h.axis, hi)} ({breaks.label(ncl)}: "
        f"{_fmt(bad_run.measured.get(h.claim, float('nan')), m.unit)})")


def _axis_fmt(axis: str, v: float) -> str:
    return {"vdd": f"{v:.3f}V", "temp": f"{v:.0f}C", "vcm": f"{v:.2f}*VDD",
            "cl_x": f"{v:.2f}x load"}.get(axis, f"{v:g}")


def grid(snap: Snapshot, *, emit, round_: int) -> list[Run]:
    """Brute-force corners over the envelope: the adversary's baseline."""
    conds = [
        Condition(c, v, t, snap.cl, 0.5)
        for c in CORNERS for v in (ENVELOPE["vdd"][0], snap.pdk.vdd, ENVELOPE["vdd"][1])
        for t in (ENVELOPE["temp"][0], 27.0, ENVELOPE["temp"][1])
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(snap.simulate, conds))
    emit({"type": "grid", "round": round_, "runs": len(runs)})
    return runs


SHMOO_VDD = (1.5, 1.6, 1.71, 1.8, 1.89, 2.0)     # envelope edges on the grid
SHMOO_TEMP = (-40.0, 27.0, 85.0, 125.0)     # the grid's temperatures reuse its runs


def shmoo(snap: Snapshot, label: str, *, emit=None) -> dict:
    """Pass/fail over supply x temperature, per process corner.

    The chart test floors draw for real silicon: each cell is one forked
    simulation, marked with the claims that failed there. The operating
    envelope is drawn on top, so a red cell inside it is a broken promise
    and a red cell outside it is a published limit.
    """
    conds = [Condition(c, v, t, snap.cl, 0.5)
             for c in CORNERS for v in SHMOO_VDD for t in SHMOO_TEMP]
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(snap.simulate, conds))
    claims = [m.name for m in claims_of(snap.spec)]
    cells = {}
    for cond, run in zip(conds, runs):
        cells.setdefault(cond.corner, []).append({
            "vdd": cond.vdd, "temp": cond.temp, "run": run.id,
            "fails": [c for c in claims if not snap.holds(c, run)],
        })
    out = {"label": label, "design_hash": snap.hash, "vdd": list(SHMOO_VDD),
           "temp": list(SHMOO_TEMP), "corners": list(CORNERS), "cells": cells,
           "envelope": ENVELOPE, "sims": len(conds)}
    if emit:
        emit({"type": "shmoo", **out})
    return out


# ---------------------------------------------------------------------------


@dataclass
class Assessment:
    round: int
    design_hash: str
    nominal_run: str
    findings: list[Finding]
    grid_violations: dict[str, list[str]]       # claim -> run ids (in envelope)
    robustness: list[dict]
    adversary_sims: int
    grid_sims: int
    grid_corners: int = 0

    def to_dict(self) -> dict:
        return {
            "round": self.round, "design_hash": self.design_hash,
            "nominal_run": self.nominal_run,
            "findings": [f.to_dict() for f in self.findings],
            "grid_violations": self.grid_violations,
            "robustness": self.robustness,
            "adversary_sims": self.adversary_sims, "grid_sims": self.grid_sims,
            "grid_corners": self.grid_corners,
        }


def assess(snap: Snapshot, hyps: list[Hypothesis], *, emit, round_: int) -> Assessment:
    ncl = snap.cl
    nominal = snap.simulate(snap.nominal)
    before = len(snap.runs)
    findings = []
    for h in hyps:
        emit({"type": "hypothesis", "round": round_, **h.to_dict()})
        f = attack(snap, h, emit=emit, round_=round_)
        emit({"type": "finding", "round": round_, **f.to_dict()})
        findings.append(f)
    adversary_sims = len(snap.runs) - before

    before = len(snap.runs)
    runs = grid(snap, emit=emit, round_=round_)
    grid_sims = len(snap.runs) - before

    claims = claims_of(snap.spec)
    violations = {m.name: [r.id for r in runs if not snap.holds(m.name, r)] for m in claims}

    robustness = []
    for m in claims:
        nom = nominal.measured.get(m.name, float("nan"))
        env_runs = [r for r in snap.runs.values() if r.condition.in_envelope(ncl)]
        worst = _worst(m, env_runs)
        f_for = [f for f in findings if f.hypothesis.claim == m.name]
        limits = [f.statement for f in f_for if f.verdict in ("boundary", "broken")
                  and not f.in_envelope]
        broken_in = [f for f in f_for if f.verdict in ("boundary", "broken") and f.in_envelope]
        status = "holds"
        if violations[m.name] or broken_in:
            status = "violated"
        robustness.append({
            "claim": m.name, "unit": m.unit,
            "relation": ">=" if m.direction == "max" else "<=",
            "target": m.target, "nominal": nom,
            "worst": worst.measured.get(m.name) if worst else None,
            "worst_condition": worst.condition.label(ncl) if worst else "",
            "worst_run": worst.id if worst else None,
            "status": status,
            "violations_in_envelope": len(violations[m.name])
                                      + sum(1 for f in broken_in if f.breaking_run not in violations[m.name]),
            "limits": limits,
            "counterexamples": sorted({f.breaking_run for f in f_for if f.breaking_run}
                                      | set(violations[m.name][:3])),
            "missed_by_adversary": bool(violations[m.name]) and not broken_in,
        })
    return Assessment(round_, snap.hash, nominal.id, findings, violations, robustness,
                      adversary_sims, grid_sims, len(runs))


def _worst(m, runs: list[Run]) -> Run | None:
    vals = [(r.measured.get(m.name, float("nan")), r) for r in runs if r.ok]
    vals = [(v, r) for v, r in vals if math.isfinite(v)]
    if not vals:
        return None
    return (min if m.direction == "max" else max)(vals, key=lambda x: x[0])[1]


def breaking_conditions(snap: Snapshot, a: Assessment, limit: int = 4) -> list[Condition]:
    """In-envelope conditions the repair must also satisfy: for every broken
    claim, the single worst condition anything -- hypothesis or grid -- found."""
    by_id = {r.id: r for r in snap.runs.values()}
    conds: list[Condition] = []
    for row in a.robustness:
        if row["status"] != "violated" or row["worst_run"] not in by_id:
            continue
        cond = by_id[row["worst_run"]].condition
        if cond not in conds:
            conds.append(cond)
    return conds[:limit]


def run_record(r: Run, nominal_cl: float) -> dict:
    return {"id": r.id, "condition": r.condition.label(nominal_cl),
            "in_envelope": r.condition.in_envelope(nominal_cl),
            "measured": {k: (v if math.isfinite(v) else None) for k, v in r.measured.items()
                         if k in ("gain", "gbw", "pm", "pwr", "vout_dc")},
            "ok": r.ok, "deck": r.deck, "log": r.log, "design_hash": r.design_hash}
