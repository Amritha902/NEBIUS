"""tapeout.verify: recorded evidence re-simulates, and tampering is caught."""

import copy
import json
import shutil
from pathlib import Path

import pytest

from bias import pdk
from tapeout import verify

REC = Path(__file__).resolve().parent.parent / "web" / "replays" / "ppg-offline.jsonl"

needs_sky130 = pytest.mark.skipif(
    not shutil.which("ngspice") or "sky130" not in pdk.available() or not REC.exists(),
    reason="needs ngspice, the sky130 models and the bundled recording",
)


def test_decks_are_pointed_at_the_local_pdk():
    deck = ".include /somewhere/else/pdks/sky130/cells/x.spice\nM1 a b c d nch\n"
    out = verify.localise(deck)
    assert f".include {pdk.PDK_DIR}/sky130/cells/x.spice" in out
    assert "M1 a b c d nch" in out


def test_undefined_measurements_compare_equal():
    assert verify._close(None, float("nan"))
    assert not verify._close(None, 1.0)
    assert verify._close(60.0, 60.00001) and not verify._close(60.0, 61.0)


def _one_run():
    result = verify.load(REC)
    return next(r for c in result["campaigns"] for r in c["runs"].values() if r.get("deck"))


@needs_sky130
def test_a_recorded_deck_reproduces():
    assert verify.check(_one_run())["match"]


@needs_sky130
def test_a_doctored_number_is_caught():
    run = copy.deepcopy(_one_run())
    run["measured"]["gain"] += 3.0                 # claim 3 dB the circuit never had
    got = verify.check(run)
    assert not got["match"]
    assert [r["metric"] for r in got["rows"] if not r["match"]] == ["gain"]


@needs_sky130
def test_a_doctored_deck_is_caught():
    run = copy.deepcopy(_one_run())
    run["deck"] = run["deck"].replace("CL   vout 0 2e-12", "CL   vout 0 2e-11")   # 10x load
    assert "2e-11" in run["deck"]
    assert not verify.check(run)["match"]


@needs_sky130
def test_cli_on_a_tampered_file_fails(tmp_path, capsys):
    result = verify.load(REC)
    camp = result["campaigns"][0]
    keep = {k: v for k, v in camp["runs"].items() if v.get("deck")}
    first = next(iter(keep))
    camp["runs"] = {first: keep[first]}
    camp["runs"][first]["measured"]["gbw"] *= 2
    f = tmp_path / "run.json"
    f.write_text(json.dumps(result))
    assert verify.main([str(f)]) == 1
    assert "did NOT reproduce" in capsys.readouterr().out
