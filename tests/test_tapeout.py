"""Tests for the Tapeout layer: compiler, auditor, falsifier, servo."""

import os

import pytest

from bias import pdk, specparse, topology
import shutil
from tapeout import architect, falsify, writer

os.environ.setdefault("TAPEOUT_OFFLINE", "1")

needs_sky130 = pytest.mark.skipif(
    not shutil.which("ngspice") or "sky130" not in pdk.available(),
    reason="needs ngspice and the sky130 models",
)


# -- the compiler the architect is tested against ----------------------------

def _plan(*blocks):
    return {"part_number": "TPO-TEST", "name": "t", "blocks": list(blocks)}


def test_compiler_accepts_a_sound_plan():
    plan, errors = architect.compile_plan(_plan(
        {"id": "afe", "kind": "analog", "spec": "op-amp with 60dB gain, 2MHz bandwidth, under 200uW, 2pF load"},
        {"id": "cmp", "kind": "digital", "cell": "comparator1"},
    ))
    assert errors == []
    assert plan.blocks[0].topology == "miller"
    assert any(t["name"] == "load" for t in plan.blocks[0].targets)


@pytest.mark.parametrize("block, needle", [
    ({"id": "a", "kind": "analog", "spec": "op-amp with 120dB gain"}, "beyond"),
    ({"id": "a", "kind": "analog", "spec": "a nice amplifier"}, "no targets"),
    ({"id": "a", "kind": "analog", "spec": "op-amp with 40dB gain and 200MHz bandwidth"}, "envelope"),
    ({"id": "a", "kind": "digital", "cell": "fft_engine"}, "unknown cell"),
])
def test_compiler_rejects_with_a_reason_the_model_can_act_on(block, needle):
    plan, errors = architect.compile_plan(_plan(block))
    assert plan is None
    assert any(needle in e for e in errors), errors


def test_compiler_requires_an_analog_block():
    plan, errors = architect.compile_plan(_plan({"id": "d", "kind": "digital", "cell": "xor2"}))
    assert plan is None and errors


def test_offline_plan_compiles():
    assert architect.offline_plan("anything").blocks


# -- the auditor ----------------------------------------------------------------

def test_auditor_strikes_numbers_with_no_source():
    known = {"dB": [63.2], "Hz": [5.13e6], "W": [79.3e-6], "count": [34]}
    assert writer.audit_text("63.2 dB of gain and 5.1 MHz GBW at 79uW", known) == []
    bad = writer.audit_text("80 dB gain, 2 nV/rtHz noise, 12 transistors", known)
    assert "80 dB" in bad
    assert any("12" in b for b in bad)


# -- the falsifier --------------------------------------------------------------

def test_envelope_classification():
    cl = 2e-12
    assert falsify.Condition("ff", 1.71, 85, cl, 0.5).in_envelope(cl)
    assert not falsify.Condition("tt", 1.6, 27, cl, 0.5).in_envelope(cl)
    assert not falsify.Condition("tt", 1.8, 125, cl, 0.5).in_envelope(cl)
    assert not falsify.Condition("tt", 1.8, 27, 3 * cl, 0.5).in_envelope(cl)


def test_hypotheses_are_clipped_and_validated():
    raw = [
        {"claim": "pm", "fixed": {"corner": "FF", "temp": 999}, "axis": "cl_x", "toward": 50},
        {"claim": "noise", "axis": "vdd", "toward": 1.6},           # not a claim we make
        {"claim": "gain", "fixed": {"corner": "xx"}, "axis": "warp", "toward": 3},
    ]
    hs = falsify._clean_hypotheses(raw, {"pm", "gain"}, "nemotron")
    assert [h.claim for h in hs] == ["pm", "gain"]
    assert hs[0].fixed == {"corner": "ff", "temp": 150.0}
    assert hs[0].toward == 10.0
    assert hs[1].axis == "none" and hs[1].fixed == {}


def test_heuristics_cover_every_claim():
    hs = falsify.heuristic_hypotheses(set(falsify.CLAIMS))
    assert {h.claim for h in hs} == set(falsify.CLAIMS)


# -- simulation -----------------------------------------------------------------

@needs_sky130
def test_servo_holds_the_output_at_its_operating_point():
    """Without the servo a 5% supply change rails an open-loop amplifier."""
    p = pdk.get("sky130")
    t = topology.get("ota5t")
    from bias.sim import run_deck
    tb = topology.Testbench(cl=1e-12, servo=True)
    m = t.postprocess(run_deck(t.deck(p, t.defaults(), tb)).values, t.defaults(), p)
    assert abs(m["vout_dc"] - 0.9) < 0.1
    assert m["gain"] > 20


@needs_sky130
def test_a_corner_fork_never_mutates_the_nominal_design():
    p = pdk.get("sky130")
    t = topology.get("ota5t")
    spec = specparse.parse("op-amp with 30dB gain, 1MHz bandwidth, under 500uW, 1pF load").spec
    snap = falsify.Snapshot(t, t.defaults(), p, 1e-12, spec)
    before = dict(snap.values)
    nominal = snap.simulate(snap.nominal)
    hot = snap.simulate(falsify.Condition("ss", 1.71, 85.0, 1e-12, 0.5))
    assert snap.values == before
    assert nominal.id != hot.id
    assert "__ss.corner" in hot.deck and "__tt.corner" in nominal.deck
    assert hot.measured["gbw"] != nominal.measured["gbw"]


def test_repair_targets_the_worst_run_not_the_first_failure():
    spec = specparse.parse("op-amp with 60dB gain, 2MHz bandwidth, 2pF load").spec
    snap = falsify.Snapshot(topology.get("miller"), topology.get("miller").defaults(),
                            pdk.DEV180, 2e-12, spec)
    mild = falsify.Condition("tt", 1.71, 27.0, 2e-12, 0.5)
    awful = falsify.Condition("sf", 1.71, -40.0, 2e-12, 0.5)
    for i, cond in enumerate((mild, awful)):
        snap.runs[cond] = falsify.Run(f"r{i}", cond, {}, True, "", "", snap.hash)
    a = falsify.Assessment(1, snap.hash, "r0", [], {}, [
        {"claim": "gbw", "status": "violated", "worst_run": "r1", "counterexamples": ["r0", "r1"]},
        {"claim": "gain", "status": "holds", "worst_run": "r0", "counterexamples": []},
    ], 0, 0)
    assert falsify.breaking_conditions(snap, a) == [awful]
