"""The live Token Factory path, exercised against a protocol-faithful mock.

Everything a real key would run -- model discovery, streaming, reasoning
capture, repair loops, the auditor's rewrite, the sizing lane's call cap --
runs here against tests/mock_tokenfactory.py. Only the remote server itself
is not under test.
"""

import json

import pytest

from tapeout import architect, fab, llm

from . import mock_tokenfactory as mock


@pytest.fixture
def live(monkeypatch):
    srv = mock.serve(0)
    monkeypatch.setattr(llm, "BASE_URL", f"http://127.0.0.1:{srv.server_port}/v1")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.delenv("TAPEOUT_OFFLINE", raising=False)
    monkeypatch.setattr(llm, "_discovered", False)
    llm.MODELS.clear()
    mock.CALLS.clear()
    mock.FAULT = ""
    yield
    mock.FAULT = ""
    srv.shutdown()
    llm.MODELS.clear()
    llm._discovered = False


@pytest.mark.parametrize("cut", range(1, 40))
def test_think_tags_split_anywhere_are_still_separated(cut):
    raw = '<think>plan it</think>{"a": 1}'
    sp = llm.ThinkSplitter()
    got = sp.feed(raw[:cut]) + sp.feed(raw[cut:]) + sp.flush()
    text = "".join(t for k, t in got if k == "text")
    think = "".join(t for k, t in got if k == "think")
    assert text == '{"a": 1}' and think == "plan it"


def test_discovery_resolves_case_and_tiers(live):
    m = llm.discover()
    assert m["ultra"] == "nvidia/Nemotron-3-Ultra-550B-A55B"     # case from the live list
    assert m["super"] == "nvidia/nemotron-3-super-120b-a12b"
    assert m["nano"] == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


def test_stream_captures_reasoning_answer_and_usage(live):
    ledger = llm.Ledger()
    seen = []
    answer, think = llm.chat("ultra", [
        {"role": "system", "content": "You are the red-team engineer"},
        {"role": "user", "content": "go"}], ledger=ledger,
        on_delta=lambda k, t: seen.append(k))
    assert json.loads(answer)["hypotheses"]
    assert "Margins" in think
    assert "think" in seen and "text" in seen
    assert ledger.summary()[0]["prompt_tokens"] == 100


def test_malformed_chunk_is_skipped(live):
    mock.FAULT = "garbage"
    answer, _ = llm.chat("nano", [{"role": "user", "content": 'Reply with the JSON object {"ok": true}'}])
    assert json.loads(answer) == {"ok": True}


def test_dropped_stream_is_an_error_not_a_short_answer(live, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    mock.FAULT = "disconnect"
    with pytest.raises(llm.LLMError, match="stream ended"):
        llm.chat("nano", [{"role": "user", "content": 'Reply with the JSON object {"ok": true}'}])


def test_architect_repairs_a_rejected_plan(live):
    events = []
    plan = architect.design_chip("a PPG front-end", "(none)", ledger=llm.Ledger(),
                                 emit=events.append)
    assert plan.by_model and not plan.fallback
    assert len(plan.repairs) == 1 and "beyond" in plan.repairs[0]["errors"][0]
    assert plan.blocks[0].spec.startswith("op-amp with 60dB")
    assert any(e["type"] == "token" and e["kind"] == "think" for e in events)


def test_writer_audit_forces_a_rewrite(live):
    from bias import pdk
    from tapeout import writer
    plan = architect.offline_plan("x")
    events = []
    out = writer.write(plan, [], [], pdk.DEV180, ledger=llm.Ledger(), emit=events.append)
    first = next(e for e in events if e["type"] == "audit")
    assert {f["claim"] for f in first["flagged"]} >= {"140 dB"}
    assert out["by_model"] and out["rewrites"] == 1 and out["flagged"] == []


def test_sizing_lane_cannot_loop_forever(live):
    from bias.evaluate import BudgetExhausted
    client = fab._Client(llm.Ledger(), on_turn=lambda r: None, max_calls=3)
    sys_prompt = "You are an expert analog IC designer sizing a circuit against a spec."
    for _ in range(3):
        client.messages.create(model="", max_tokens=100, system=sys_prompt,
                               messages=[{"role": "user", "content": "  w1: x [1e-6 .. 1e-5] m"}])
    with pytest.raises(BudgetExhausted):
        client.messages.create(model="", max_tokens=100, system=sys_prompt,
                               messages=[{"role": "user", "content": "x"}])


def test_scout_keeps_only_citations_it_retrieved(live, monkeypatch):
    from tapeout import research
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(research, "search", lambda q, max_results=5: [
        {"title": "PPG basics", "url": "https://example.org/ppg", "content": "AC part ~1 mV"}])
    f = research.scout("a PPG front-end", ledger=llm.Ledger(), emit=lambda e: None)
    assert f.queries == ["PPG signal bandwidth amplitude", "PPG AFE power"]
    assert [x.source for x in f.facts] == ["https://example.org/ppg"]   # invented URL dropped
