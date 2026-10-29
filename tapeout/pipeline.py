"""One click: a product idea in, a verified chip datasheet out.

    scout      Nemotron Nano + Tavily   what does the product need?
    architect  Nemotron Ultra           what silicon does that take?
    compiler   deterministic            can the fab build it? (else: repair)
    fab        Nemotron Super vs DE     size every analog block on ngspice
               ngspice                  prove every logic block's truth table
    writer     Nemotron Nano            the datasheet prose
    auditor    deterministic            every number traced to a simulation

Every stage reports through `emit`, which the web server turns into a live
event stream and the CLI turns into a log.
"""

from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from bias import pdk as pdk_registry

from bias import specparse
from bias import topology as topo_registry

from . import architect, fab, falsify, llm, research, writer

MAX_REPAIRS = 2
MAX_REPAIR_CONDITIONS = 5


def default_pdk():
    try:
        return pdk_registry.get("sky130")
    except (KeyError, FileNotFoundError):
        return pdk_registry.DEV180


def tapeout(idea: str, emit, *, pdk=None) -> dict:
    t0 = time.perf_counter()
    ledger = llm.Ledger()
    pdk = pdk or default_pdk()
    models = llm.discover()

    emit({"type": "hello", "online": llm.online(), "models": models,
          "tavily": bool(research.tavily_key()), "pdk": pdk.name,
          "calibrated": pdk.calibrated, "idea": idea})

    def stage(agent, status, **kw):
        emit({"type": "stage", "agent": agent, "status": status, **kw})

    # Which agents actually ran on Nemotron, and why any did not. A fallback
    # that looks like a live run would make the model look cosmetic, so every
    # stand-in is declared, on screen and on the datasheet.
    provenance: list[dict] = []

    def prov(agent, live, model, detail):
        row = {"agent": agent, "live": bool(live), "model": model if live else "",
               "detail": detail}
        provenance.append(row)
        emit({"type": "provenance", **row})

    # 1. Scout
    stage("scout", "running", model=models["nano"])
    findings = research.scout(idea, ledger=ledger, emit=emit)
    stage("scout", "done", facts=len(findings.facts), note=findings.note)
    prov("scout", llm.online() and findings.facts, models["nano"],
         f"{len(findings.facts)} cited facts from {len(findings.sources)} Tavily results"
         if findings.facts else (findings.note or ("no NEBIUS_API_KEY" if not llm.online()
                                                   else "no facts extracted")))
    emit({"type": "research", **findings.to_dict()})

    # 2-3. Architect, checked by the compiler.
    stage("architect", "running", model=models["ultra"])
    try:
        plan = architect.design_chip(idea, findings.brief(), ledger=ledger, emit=emit)
    except llm.LLMError as exc:
        emit({"type": "log", "agent": "architect", "msg": f"model call failed: {exc}"})
        plan = architect.offline_plan(idea, f"model call failed: {str(exc)[:160]}")
    stage("architect", "done", repairs=len(plan.repairs))
    prov("architect", plan.by_model, models["ultra"],
         plan.fallback or f"plan compiled after {len(plan.repairs)} repair round(s)")
    emit({"type": "plan", "plan": plan.to_dict()})

    # 4. Fab. Logic blocks are proved in parallel with the analog race; the
    # analog blocks run one after another so each race is legible.
    stage("fab", "running", model=models["super"])
    stage("compiler", "done", repairs=len(plan.repairs))
    analog_blocks = [b for b in plan.blocks if b.kind == "analog"]
    digital_blocks = [b for b in plan.blocks if b.kind == "digital"]

    analog, digital = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        def prove_all():
            out = []
            for b in digital_blocks:
                emit({"type": "verify_start", "block": b.id, "cell": b.cell})
                d = fab.build_digital(b, pdk, emit=emit)
                emit({"type": "block_done", "block": d.to_dict()})
                out.append(d)
            return out

        dig_future = pool.submit(prove_all)
        for b in analog_blocks:
            a = fab.build_analog(b, pdk, ledger=ledger, emit=emit)
            emit({"type": "block_done", "block": a.to_dict()})
            analog.append(a)
        digital = dig_future.result()
    stage("fab", "done")
    for a in analog:
        lane = a.lanes["nemotron"]
        prov(f"fab:{a.block.id}", lane.sims > 0 and not lane.error, models["super"],
             lane.error or f"Nemotron proposed {lane.sims} sizings"
             + (" and won the race" if a.winner == "nemotron" else
                "; evolution met the spec first"))

    # 5. Red team: try to break every analog claim, repair what breaks
    # inside the envelope, and attack the repair again.
    stage("redteam", "running", model=models["ultra"])
    campaigns = []
    for i, a in enumerate(analog):
        fixed, campaign = red_team(a, pdk, ledger=ledger, emit=emit)
        analog[i] = fixed
        campaigns.append(campaign)
        if fixed is not a:
            emit({"type": "block_done", "block": fixed.to_dict()})
    stage("redteam", "done")
    for c in campaigns:
        by = [h["by"] for h in c["hypotheses"]]
        n = by.count("nemotron")
        prov(f"redteam:{c['block']}", n > 0, models["ultra"],
             f"{n} of {len(by)} hypotheses from Nemotron"
             + ("" if n == len(by) else
                f"; {len(by) - n} textbook stand-ins"
                + (" (no NEBIUS_API_KEY)" if not llm.online() else " (model reply unusable)")))

    # 5-6. Writer and auditor.
    stage("writer", "running", model=models["nano"])
    sheet = writer.write(plan, analog, digital, pdk, ledger=ledger, emit=emit,
                         campaigns=campaigns)
    stage("writer", "done", flagged=len(sheet["flagged"]), rewrites=sheet["rewrites"])
    prov("writer", sheet["by_model"], models["nano"],
         f"prose audited; {len(sheet['flagged'])} figure(s) struck" if sheet["by_model"]
         else ("template prose (no NEBIUS_API_KEY)" if not llm.online()
               else "template prose (model call failed)"))

    sims = sum(l.sims for a in analog for l in a.lanes.values())
    sims += sum(c["sims"] for c in campaigns)
    verified = all(a.solved for a in analog) and all(d.passed for d in digital)
    robust = all(not c["open_violations"] for c in campaigns)
    result = {
        "type": "done",
        "idea": idea,
        "plan": plan.to_dict(),
        "research": findings.to_dict(),
        "analog": [a.to_dict() for a in analog],
        "digital": [d.to_dict() for d in digital],
        "datasheet": sheet,
        "campaigns": campaigns,
        "provenance": provenance,
        "live": bool(provenance) and all(p["live"] for p in provenance),
        "robust": robust,
        "usage": ledger.summary(),
        "pdk": pdk.name,
        "vdd": pdk.vdd,
        "online": llm.online(),
        "verified": verified,
        "simulations": sims + sum(1 for _ in digital),
        "transistors": sum(d.transistors for d in digital)
                       + sum(6 if a.topology == "ota5t" else 8 for a in analog),
        "seconds": round(time.perf_counter() - t0, 1),
    }
    emit(result)
    return result


def safe_tapeout(idea: str, emit, **kw) -> dict | None:
    try:
        return tapeout(idea, emit, **kw)
    except Exception as exc:                       # report, never hang the stream
        emit({"type": "error", "msg": f"{type(exc).__name__}: {exc}",
              "trace": traceback.format_exc()[-1500:]})
        return None


def red_team(a: "fab.AnalogOutcome", pdk, *, ledger, emit):
    """Attack one amplifier until its claims hold or its limits are known.

    Returns the (possibly repaired) outcome and the campaign record: every
    round's findings, the robustness table, and the runs behind them.
    """
    block = a.block
    parsed = specparse.parse(block.spec)
    cl = parsed.cl or 1e-12

    def snapshot(outcome):
        return falsify.Snapshot(topo_registry.get(outcome.topology), outcome.values,
                                pdk, cl, parsed.spec)

    snap = snapshot(a)
    emit({"type": "redteam_start", "block": block.id, "design_hash": snap.hash})
    nominal = snap.simulate(snap.nominal)
    hyps = falsify.propose(snap, nominal, ledger=ledger, emit=emit)
    emit({"type": "hypotheses", "block": block.id,
          "hypotheses": [h.to_dict() for h in hyps]})

    snaps = [snap]
    rounds = [falsify.assess(snap, hyps, emit=emit, round_=1)]
    emit({"type": "assessment", "block": block.id, **rounds[-1].to_dict()})
    current = a
    repairs = []
    # Every condition that ever broke a claim stays in the repair set: a fix
    # for round 2's corners must not reopen round 1's. (It did, before this.)
    regressions: list = []
    for r in range(1, MAX_REPAIRS + 1):
        fresh_conds = falsify.breaking_conditions(snap, rounds[-1])
        if not fresh_conds or not current.solved:
            break
        conds = (regressions + [c for c in fresh_conds if c not in regressions])[-MAX_REPAIR_CONDITIONS:]
        regressions = conds
        emit({"type": "repair_start", "block": block.id, "round": r,
              "conditions": [c.label(cl) for c in conds]})
        fixed = fab.repair(current, pdk, conds, ledger=ledger, emit=emit, round_=r)
        if fixed is None or not fixed.solved:
            repairs.append({"round": r, "conditions": [c.label(cl) for c in conds],
                            "result": "no sizing met every condition within budget"})
            emit({"type": "repair_done", "block": block.id, "round": r, "ok": False})
            break
        repairs.append({"round": r, "conditions": [c.label(cl) for c in conds],
                        "result": f"re-sized by {fixed.winner}",
                        "winner": fixed.winner})
        emit({"type": "repair_done", "block": block.id, "round": r, "ok": True,
              "winner": fixed.winner})
        current = fixed
        snap = snapshot(current)
        snaps.append(snap)
        history = "\n".join(
            f"Round {rd.round} broke: " + ", ".join(
                f"{row['claim']} (worst {row['worst']:.4g} at {row['worst_condition']})"
                for row in rd.robustness if row["status"] == "violated" and row["worst"] is not None)
            for rd in rounds)
        fresh = falsify.propose(snap, snap.simulate(snap.nominal), ledger=ledger, emit=emit,
                                previous=hyps, history=history)
        if fresh:
            emit({"type": "hypotheses", "block": block.id, "round": r + 1, "new": True,
                  "hypotheses": [h.to_dict() for h in fresh]})
            hyps = hyps + fresh
        rounds.append(falsify.assess(snap, hyps, emit=emit, round_=r + 1))
        emit({"type": "assessment", "block": block.id, **rounds[-1].to_dict()})

    # Keep the full trace (deck + ngspice log) for every run a finding or a
    # table cell links to; the rest keep their measurements only.
    linked = set()
    for rd in rounds:
        linked.add(rd.nominal_run)
        for f in rd.findings:
            linked.update(f.runs)
        for row in rd.robustness:
            linked.update(row["counterexamples"])
            if row["worst_run"]:
                linked.add(row["worst_run"])
    runs = {}
    for sn in snaps:
        for run in sn.runs.values():
            rec = falsify.run_record(run, cl)
            if run.id not in linked:
                rec.pop("deck"), rec.pop("log")
            runs[run.id] = rec

    final = rounds[-1]
    campaign = {
        "block": block.id,
        "title": block.title,
        "hypotheses": [h.to_dict() for h in hyps],
        "rounds": [rd.to_dict() for rd in rounds],
        "repairs": repairs,
        "robustness": final.robustness,
        "open_violations": [row["claim"] for row in final.robustness
                            if row["status"] == "violated"],
        "runs": runs,
        "sims": sum(len(sn.runs) for sn in snaps),
        "envelope": falsify.ENVELOPE,
        "nominal_cl": cl,
    }
    emit({"type": "campaign_done", "block": block.id,
          "open_violations": campaign["open_violations"],
          "rounds": len(rounds), "sims": campaign["sims"]})
    return current, campaign
