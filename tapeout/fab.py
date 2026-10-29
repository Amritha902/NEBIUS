"""The fab floor: every block built, and proved by simulation.

Analog blocks are sized in a race. Two designers work the same spec against
the same ngspice and the same SkyWater 130nm models, each with its own
simulation counter:

  nemotron   -- Nemotron Super, reasoning like a designer at a terminal: read
                the measured gain/GBW/power, decide what to change and why.
  evolution  -- differential evolution, the strongest classical baseline in
                the engine. No reasoning, many simulations.

The first to meet every target stops the other. Both are streamed live, so
the race is visible: the agent's reasoning next to each simulation it
spends, and the evolutionary search's cloud of samples beside it.

Digital blocks are synthesised deterministically and proved against their
truth table by transient simulation. Nothing here trusts a model's claim
that a circuit works -- only the simulator gets to say that.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from bias import logic, optimizers, plot, specparse, symbol, verify
from bias import topology as topo_registry
from bias.agent import LLMAgent
from bias.evaluate import BudgetExhausted, Evaluation, Evaluator, TargetMet
from bias.sim import run_deck
from bias.pdk import PDK
from bias.topology import Testbench

from . import llm
from .architect import Block

NEMOTRON_BUDGET = 14
DE_BUDGET = 300
DE_ATTEMPTS = 2
# Measured: re-sizing a two-stage op-amp against nominal + its worst corners
# needs ~480 candidates from a random start, so repairs get a larger budget
# and start from the design that broke rather than from scratch.
REPAIR_BUDGET = 500


class WarmDE(optimizers.DifferentialEvolution):
    """Differential evolution with the broken design planted in generation 0.

    A repair is a small move from a sizing that already met spec at typical:
    seeding the population with it, plus jittered copies, starts the search
    next to the answer instead of uniformly across a 13-dimensional box.
    """

    def __init__(self, seed_values: dict[str, float], **kw):
        super().__init__(**kw)
        self.seed_values = seed_values

    def run(self, ev, rng):
        params = ev.topology.params
        anchor = np.array([p.to_unit(self.seed_values[p.name]) for p in params])
        n = len(params)
        pop_size = max(5, min(self.pop_size, max(5, ev.budget // 4)))
        try:
            pop = rng.random((pop_size, n))
            pop[0] = anchor
            k = pop_size // 2
            pop[1:k] = np.clip(anchor + rng.normal(0, 0.08, (k - 1, n)), 0, 1)
            fit = np.array([self._score(ev, q) for q in pop])
            while not ev.exhausted:
                for i in range(pop_size):
                    if ev.exhausted:
                        break
                    idxs = [j for j in range(pop_size) if j != i]
                    a, b, c = pop[rng.choice(idxs, 3, replace=False)]
                    mutant = np.clip(a + self.f * (b - c), 0, 1)
                    cross = rng.random(n) < self.cr
                    if not cross.any():
                        cross[rng.integers(n)] = True
                    trial = np.where(cross, mutant, pop[i])
                    ft = self._score(ev, trial)
                    if ft <= fit[i]:
                        pop[i], fit[i] = trial, ft
        except BudgetExhausted:
            pass


class RaceEvaluator(Evaluator):
    """An Evaluator that reports every simulation and can be stopped."""

    def evaluate(self, values):
        if self._stop.is_set():
            raise BudgetExhausted("the other racer met the spec")
        e = super().evaluate(values)
        if not e.cached:
            self._on_eval(e)
        return e


class MultiConditionEvaluator(Evaluator):
    """Scores a sizing at several operating conditions at once.

    Used by the repair: a design that broke at a corner is re-sized against
    the nominal point and every condition that broke it, and the worst of
    them is its score. One "simulation" here is one candidate, however many
    conditions it is checked at, so the counter stays comparable.
    """

    def evaluate(self, values):
        clipped = {p.name: p.clip(float(values[p.name])) for p in self.topology.params}
        key = tuple(round(clipped[p.name], 15) for p in self.topology.params)
        if key in self._cache:
            prior = self._cache[key]
            hit = dataclasses.replace(prior, seconds=0.0, cached=True)
            self.history.append(hit)
            return hit
        if self.stop_on_success and self.solved() is not None:
            raise TargetMet("every constraint is already satisfied")
        if self.exhausted:
            raise BudgetExhausted(f"simulation budget of {self.budget} is spent")

        t0 = time.perf_counter()

        def one(cond):
            cpdk, tb = cond
            res = run_deck(self.topology.deck(cpdk, clipped, tb), timeout=self.timeout)
            m = self.topology.postprocess(res.values, clipped, cpdk)
            return res.ok, m, self.spec.score(m), res.ok and self.spec.satisfied(m)

        with ThreadPoolExecutor(max_workers=len(self._conditions)) as pool:
            per = list(pool.map(one, self._conditions))
        ok, measured, score, _ = max(per, key=lambda r: r[2])
        ev = Evaluation(
            index=self.used + 1, values=clipped, measured=measured, score=score,
            satisfied=all(r[3] for r in per), ok=all(r[0] for r in per),
            seconds=time.perf_counter() - t0,
        )
        self._cache[key] = ev
        self.history.append(ev)
        return ev


class RaceMultiEvaluator(RaceEvaluator, MultiConditionEvaluator):
    pass


def _racer_evaluator(topo, pdk, spec, tb, budget, stop, on_eval,
                     conditions=None) -> RaceEvaluator:
    cls = RaceMultiEvaluator if conditions else RaceEvaluator
    ev = cls(topo, pdk, spec, tb, budget=budget, stop_on_success=True)
    ev._stop = stop
    ev._on_eval = on_eval
    ev._conditions = conditions
    return ev


class _TokenFactoryMessages:
    """Lets the engine's LLM sizing agent run on Nemotron via Token Factory.

    bias.agent.LLMAgent was written against an Anthropic-style client. This
    adapter presents that shape and routes the call to Nemotron Super, and
    reports the agent's stated reasoning before its proposal is simulated.
    """

    def __init__(self, ledger, on_turn, max_calls):
        self.ledger = ledger
        self.on_turn = on_turn
        self.max_calls = max_calls
        self.calls = 0

    def create(self, *, model, max_tokens, system, messages):
        # Re-proposing an already-simulated sizing is a free cache hit for the
        # simulation budget, so without a cap on calls a model that repeats
        # itself loops (and bills) until the other racer wins.
        self.calls += 1
        if self.calls > self.max_calls:
            raise BudgetExhausted(f"Nemotron lane used its {self.max_calls} calls")
        answer, _ = llm.chat(
            "super",
            [{"role": "system", "content": system}, *messages],
            ledger=self.ledger, max_tokens=max(max_tokens, 900),
            temperature=0.4, reasoning=False, json_mode=True, timeout=90,
        )
        try:
            self.on_turn(str(llm.extract_json(answer).get("reasoning", "")))
        except ValueError:
            pass

        class _Block:
            type = "text"
            text = answer

        class _Reply:
            content = [_Block()]

        return _Reply()


class _Client:
    def __init__(self, ledger, on_turn, max_calls=2 * 14):
        self.messages = _TokenFactoryMessages(ledger, on_turn, max_calls)


@dataclass
class Lane:
    racer: str
    sims: int = 0
    solved: bool = False
    sims_to_target: int | None = None
    best_score: float = float("inf")
    seconds: float = 0.0
    error: str = ""
    best: object = None          # Evaluation


@dataclass
class AnalogOutcome:
    block: Block
    topology: str
    solved: bool
    winner: str
    lanes: dict[str, Lane]
    values: dict[str, float]
    measured: dict[str, float]
    targets: list[dict]
    netlist: str = ""
    bode_svg: str = ""
    symbol_svg: str = ""
    escalated: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.block.id,
            "kind": "analog",
            "title": self.block.title,
            "topology": self.topology,
            "solved": self.solved,
            "winner": self.winner,
            "lanes": {k: {kk: vv for kk, vv in v.__dict__.items() if kk != "best"}
                      for k, v in self.lanes.items()},
            "values": self.values,
            "measured": self.measured,
            "targets": self.targets,
            "netlist": self.netlist,
            "bode_svg": self.bode_svg,
            "symbol_svg": self.symbol_svg,
            "escalated": self.escalated,
        }


def _race(block, topo_name, pdk, parsed, *, ledger, emit, conditions=None,
          phase="design", spec=None, seed=None) -> tuple[str, dict[str, Lane]]:
    topo = topo_registry.get(topo_name)
    tb = Testbench(cl=parsed.cl or 1e-12, servo=True)
    spec = spec or parsed.spec
    stop = threading.Event()
    lanes = {"nemotron": Lane("nemotron"), "evolution": Lane("evolution")}
    winner: list[str] = []
    lock = threading.Lock()

    def on_eval_for(racer):
        def on_eval(e):
            lane = lanes[racer]
            lane.sims += 1
            if lane.best is None or e.score < lane.best.score:
                lane.best = e
                lane.best_score = e.score
            emit({
                "type": "sim", "block": block.id, "racer": racer, "phase": phase,
                "topology": topo_name, "n": lane.sims,
                "score": round(float(e.score), 5), "satisfied": e.satisfied,
                "measured": {k: float(v) for k, v in e.measured.items()
                             if np.isfinite(v)},
            })
            if e.satisfied:
                with lock:
                    if not winner:
                        winner.append(racer)
                        lane.solved = True
                        lane.sims_to_target = lane.sims
                        stop.set()
        return on_eval

    def run_nemotron():
        lane = lanes["nemotron"]
        t0 = time.perf_counter()
        try:
            client = _Client(ledger, max_calls=2 * NEMOTRON_BUDGET, on_turn=lambda r: emit({
                "type": "turn", "block": block.id, "racer": "nemotron", "phase": phase,
                "n": lane.sims + 1, "reasoning": r}))
            # A repair starts from the design that broke, the way an engineer
            # edits a schematic rather than starting over.
            agent = LLMAgent(client=client, max_tokens=700, history_window=8,
                             seed_with_defaults=seed is None)
            ev = _racer_evaluator(topo, pdk, spec, tb, NEMOTRON_BUDGET,
                                  stop, on_eval_for("nemotron"), conditions)
            if seed is not None:
                ev.evaluate(seed)
            agent.run(ev, np.random.default_rng(0))
        except BudgetExhausted:
            pass
        except Exception as exc:                          # keep the race going
            lane.error = f"{type(exc).__name__}: {exc}"[:300]
            emit({"type": "log", "agent": "sizer", "msg": f"Nemotron lane stopped: {lane.error}"})
        lane.seconds = time.perf_counter() - t0

    def run_de():
        lane = lanes["evolution"]
        t0 = time.perf_counter()
        for attempt in range(DE_ATTEMPTS):
            if stop.is_set():
                break
            ev = _racer_evaluator(topo, pdk, spec, tb,
                                  REPAIR_BUDGET if seed else DE_BUDGET, stop,
                                  on_eval_for("evolution"), conditions)
            opt = WarmDE(seed) if seed else optimizers.get("de")
            try:
                opt.run(ev, np.random.default_rng(attempt))
            except BudgetExhausted:
                pass
        lane.seconds = time.perf_counter() - t0

    threads = [threading.Thread(target=run_de, daemon=True)]
    if llm.online():
        threads.append(threading.Thread(target=run_nemotron, daemon=True))
    else:
        lanes["nemotron"].error = "offline: no NEBIUS_API_KEY"
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return (winner[0] if winner else ""), lanes


def build_analog(block: Block, pdk: PDK, *, ledger, emit) -> AnalogOutcome:
    parsed = specparse.parse(block.spec)
    gain = parsed.found.get("gain")
    topo_name = block.topology or "ota5t"
    emit({"type": "race_start", "block": block.id, "topology": topo_name,
          "why": block.topology_why, "phase": "design"})
    winner, lanes = _race(block, topo_name, pdk, parsed, ledger=ledger, emit=emit)

    escalated = ""
    best = _best(lanes)
    if (not winner and topo_name == "ota5t" and gain is not None and best is not None
            and not best.measured.get("gain", 0) >= gain):
        escalated = (f"the single stage reached only {best.measured.get('gain', float('nan')):.1f}dB "
                     f"against {gain:.0f}dB on {pdk.name}; escalated to a two-stage op-amp")
        emit({"type": "log", "agent": "sizer", "msg": escalated})
        topo_name = "miller"
        emit({"type": "race_start", "block": block.id, "topology": topo_name,
              "why": escalated, "phase": "design"})
        winner, lanes2 = _race(block, topo_name, pdk, parsed, ledger=ledger, emit=emit)
        for k, lane in lanes2.items():
            lane.sims += lanes[k].sims
        lanes = lanes2
        best = _best(lanes)

    if winner:
        best = lanes[winner].best
    values = dict(best.values) if best else {}
    measured = {k: float(v) for k, v in (best.measured if best else {}).items()}

    targets = _targets(block, parsed, measured)

    out = AnalogOutcome(block, topo_name, bool(winner), winner, lanes, values,
                        measured, targets, escalated=escalated)
    _artifacts(out, pdk, parsed)
    return out


def _targets(block, parsed, measured) -> list[dict]:
    rows = []
    for t in block.targets:
        row = dict(t)
        if t["name"] != "load":
            m = next(m for m in parsed.spec.metrics if m.name == t["name"])
            v = measured.get(t["name"], float("nan"))
            row["measured"] = v if np.isfinite(v) else None
            row["passed"] = bool(m.satisfied(v))
        rows.append(row)
    return rows


def _artifacts(out: "AnalogOutcome", pdk, parsed) -> None:
    if not out.values:
        return
    topo = topo_registry.get(out.topology)
    tb = Testbench(cl=parsed.cl or 1e-12, servo=True)
    out.netlist = topo.deck(pdk, out.values, tb)
    out.symbol_svg = symbol.Symbol(
        name=out.block.id, inputs=["vinp", "vinn"], outputs=["vout"],
        power=["vdd"], ground=["0"],
        subtitle=f"{out.topology}, {len(topo.params)} sized parameters",
    ).render()
    try:
        out.bode_svg = plot.bode(topo, pdk, out.values, tb, out.measured) or ""
    except Exception:                                      # plot is optional
        out.bode_svg = ""


def repair(out: "AnalogOutcome", pdk, conditions: list, *, ledger, emit,
           round_: int) -> "AnalogOutcome | None":
    """Re-size so the claims hold at nominal AND at every breaking condition.

    `conditions` are falsify.Condition objects. Returns a new outcome, or None
    when no sizing meeting all of them was found inside the budget.
    """
    parsed = specparse.parse(out.block.spec)
    tb_nom = Testbench(cl=parsed.cl or 1e-12, servo=True)
    conds = [(pdk, tb_nom)]
    for c in conditions:
        cpdk = dataclasses.replace(
            pdk, vdd=c.vdd,
            include=pdk.include.replace("__tt.corner", f"__{c.corner}.corner"))
        conds.append((cpdk, Testbench(cl=c.cl, temp=c.temp, vcm_frac=c.vcm, servo=True)))
    labels = [c.label(parsed.cl or 1e-12) for c in conditions]
    spec = dataclasses.replace(
        parsed.spec,
        description=parsed.spec.description + " -- must ALSO hold at: " + "; ".join(labels)
        + ". The measured numbers shown are the WORST case across these conditions.")
    emit({"type": "race_start", "block": out.block.id, "topology": out.topology,
          "why": "repair: re-sizing against nominal + " + "; ".join(labels),
          "phase": f"repair{round_}"})
    winner, lanes = _race(out.block, out.topology, pdk, parsed, ledger=ledger,
                          emit=emit, conditions=conds, phase=f"repair{round_}", spec=spec,
                          seed=out.values)
    if not winner:
        return None
    best = lanes[winner].best
    # Report the nominal-point numbers on the datasheet, not the worst case.
    nominal = Evaluator(topo_registry.get(out.topology), pdk, parsed.spec, tb_nom,
                        budget=1).evaluate(best.values)
    fixed = AnalogOutcome(out.block, out.topology, nominal.satisfied, winner, lanes,
                          dict(best.values),
                          {k: float(v) for k, v in nominal.measured.items()},
                          _targets(out.block, parsed, nominal.measured),
                          escalated=out.escalated)
    _artifacts(fixed, pdk, parsed)
    return fixed


def _best(lanes: dict[str, Lane]):
    cands = [l.best for l in lanes.values() if l.best is not None]
    return min(cands, key=lambda e: e.score) if cands else None


@dataclass
class DigitalOutcome:
    block: Block
    passed: bool
    transistors: int
    inputs: list[str]
    outputs: list[str]
    rows: list[dict] = field(default_factory=list)
    worst_rail_error: float = 0.0
    symbol_svg: str = ""
    netlist: str = ""
    reason: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "block"}
        d.update({"id": self.block.id, "kind": "digital", "title": self.block.title,
                  "cell": self.block.cell})
        return d


def build_digital(block: Block, pdk: PDK, *, emit) -> DigitalOutcome:
    t0 = time.perf_counter()
    builder, ins, outs, _ = logic.get(block.cell)
    circuit = builder()
    report = verify.verify(block.cell, pdk)
    out = DigitalOutcome(
        block, report.passed, len(circuit.transistors()), ins, outs,
        rows=[{"inputs": r.inputs, "expected": r.expected, "actual": r.actual,
               "volts": {k: round(v, 3) for k, v in r.measured_v.items()},
               "passed": r.passed} for r in report.rows],
        worst_rail_error=report.worst_rail_error,
        symbol_svg=symbol.Symbol.from_circuit(
            circuit, ins, outs,
            subtitle=f"{len(circuit.transistors())} transistors, static CMOS").render(),
        netlist=circuit.to_spice(pdk=pdk),
        reason=report.reason,
        seconds=time.perf_counter() - t0,
    )
    return out
