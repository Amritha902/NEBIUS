> **Every number on the datasheet links to the exact SPICE deck and ngspice
> log that produced it.** Tapeout writes a datasheet, then sends a red team
> to prove where it is false. What survives becomes the promise; what breaks
> becomes a published limit. Run `python -m tapeout.verify` and all 413
> recorded measurements re-simulate on your own machine.

**Status, stated plainly:** the live Nebius Token Factory path is tested end
to end against a protocol-faithful mock. Real-model runs are labelled LIVE
and offline stand-ins are labelled FALLBACK, both on screen and in the
datasheet's provenance table.

## Inspiration

LLM agents can now draft analog circuits, but a generated datasheet is a
claim, not evidence. Every demo shows a green checkmark at one operating
point: typical silicon, nominal supply, room temperature. Real chips get
judged at their corners. We wanted the agent that does the engineer's
hardest job: trying to break the claim before anyone else does.

The real problem isn't generating datasheets; AI can already do that. It's
that **nobody stands behind the numbers**. Tapeout is a generator whose
deliverable records:
- who broke which promise
- under what conditions
- with which simulation as proof

That matters for:
- **Hardware reliability:** the gap between promise and behaviour becomes
  auditable.
- **AI reliability:** every model claim is judged by an outside simulator,
  not by the model itself.
- **EDA workflows:** every counterexample becomes a permanent regression
  test.

## What it does

Type a product ("a heart-rate monitor front-end for a fitness band") and
click once. The run then goes through six stages:

1. **Scout** (Tavily + Nemotron Nano) researches the product and keeps only
   facts whose source URL it actually retrieved. It also finds a real
   commercial op-amp for the same job and puts its datasheet numbers next to
   ours. A figure is kept only if it appears verbatim in the page Tavily
   retrieved; anything else the model reports is discarded and disclosed.
2. **Architect** (Nemotron Ultra) plans the chip: amplifier specs with units,
   plus logic cells from a fixed menu. A deterministic compiler rejects
   infeasible plans and the model repairs them. No model ever writes a netlist.
3. **Fab:** Nemotron Super races differential evolution to size each
   amplifier against ngspice and SkyWater 130nm foundry models. Logic blocks
   are proved against their full truth table by transient simulation.
4. **Red team** (Nemotron Ultra) reads the sizing and margins and predicts
   the conditions where each claim should fail: process corner, supply,
   temperature, common mode, load. A deterministic lab tests each prediction
   and bisects it to the exact boundary. A 45-point corner grid audits the
   predictions.
5. A claim that breaks inside the operating envelope goes back to the design
   team for a multi-corner repair, then is attacked again. Every earlier
   counterexample stays as a regression test. A break outside the envelope
   is published as a limit.
6. **Writer** (Nemotron Nano) drafts the datasheet. An auditor strikes any
   number that no simulation backs.

The result is a datasheet with a **robustness envelope**, a **falsification
log**, and a **shmoo plot**: pass/fail over supply × temperature for every
process corner. Shown before and after repair, it shows red cells inside the
operating box turning green. Click any number to see the exact SPICE deck and raw
ngspice output behind it.

## How we built it

**The model proposes; the simulator judges.** The tiers are split by what
each task needs:
- **Nemotron Ultra** makes the two decisions where reasoning depth changes
  the outcome: the chip plan and the failure hypotheses.
- **Nemotron Super** runs the latency-bound inner loop with the simulator,
  10–20 sizing calls per block.
- **Nemotron Nano** does fast extraction and prose.

No model output is accepted on its word. Every hypothesis is tested against
ngspice and bisected 5 times to its exact boundary, and every surviving
claim is re-attacked after each repair.


- **Models:** Nebius Token Factory via its OpenAI-compatible streaming API,
  with three Nemotron tiers split by job:
  - Ultra for the two reasoning-heavy decisions, the plan and the failure
    hypotheses.
  - Super for the latency-bound inner loop with the simulator.
  - Nano for extraction and prose.
- **Engine:** our own circuit engine (deterministic synthesis, netlist
  validation, topologies) on ngspice with SkyWater sky130 corner models.
- **Forks:** each stress run is a fork of a frozen, content-hashed design,
  run in its own ngspice process. Failures become leaves in the log and
  never mutate the design.
- **Front end:** FastAPI server-sent events stream every token, simulation
  and verdict live to a single-page UI.
- **Honesty:** every agent declares whether it ran live on Nemotron or fell
  back, on screen and on the datasheet.

## Challenges we ran into

Every serious problem was a bug the falsification loop exposed in our own
work:

1. **The testbench lied.** The original engine measured amplifiers open-loop
   with no DC feedback, so a 5% supply droop made gain "collapse" from 63 dB
   to 28 dB. The falsifier would have published fake failures. A DC servo
   (a 1 GH inductor into a 1 F cap) pins the operating point and keeps the AC
   measurement open-loop. It also showed the real sky130 two-stage gain
   ceiling is about 82 dB, not the 65 dB the railed bench suggested.
2. **Repairs targeted the wrong corners.** The first repair loop took its
   conditions from the first few grid failures. Meanwhile GBW had collapsed
   to 76 kHz at sf / 1.71 V / −40 °C and phase margin to 23° at
   fs / 1.89 V / 85 °C. Repairs now target each broken claim's worst
   in-envelope run.
3. **A repair reopened an old failure.** Repair 2 fixed round 2's corners and
   broke round 1's again; the re-attack caught it. Every counterexample is
   now a permanent regression test that every later repair must pass.

Integration surprises came out of a protocol-faithful Token Factory mock:
- `<think>` tags split across stream chunks swallowed the architect's answer.
- A sizing loop could spend thousands of calls re-proposing sizings it had
  already tried.

Both are fixed and covered by tests.

## Best Use of Tavily

**Why Tavily is load-bearing: this is citation-enforced generation, not
RAG.**
- A commercial reference figure survives only if it appears verbatim, with
  the right unit, in the page Tavily retrieved.
- When the model invented "140 dB", it was discarded, and the datasheet says
  so.
- Tavily's retrieval is the audit trail that makes the reference column
  trustworthy.

Tavily does two jobs, and neither is decoration:
- **Grounding the design.** The scout's search results become cited facts
  the architect plans from. A fact whose URL was not actually retrieved is
  dropped.
- **Benchmarking it.** A second search finds the commercial op-amp a product
  team would otherwise buy. Its datasheet figures appear in a "Commercial
  ref." column next to our simulated ones, each verified verbatim against the
  retrieved page.

That turns "we designed an op-amp" into "here is the part you would buy, and
here is ours".

## Accomplishments that we're proud of

- A datasheet whose limits are found by simulation, with every number
  traceable to the run that produced it.
- No silent fallbacks: if Nemotron didn't do something, the page says so.
- 346 tests. CI builds the container and proves a sky130 simulation inside
  it.
- **The evidence re-runs anywhere.** `python -m tapeout.verify` re-simulates
  all 83 stored decks of the reference run on the checker's own machine.
  413 of 413 recorded measurements reproduce; a doctored number is caught.
  CI does this on every push.
- A recorded clean-room run (fresh clone → 340 tests passing in under 2
  minutes) is in `docs/CLEANROOM.md`.
- A judge can verify the simulation path with no keys at all:
  `python -m tapeout.check --offline`. The static replay page needs nothing.

## What we learned

Verification has to be independent of the agent that designed the thing. The
most useful thing an LLM did here was propose *where to look for failure*. The
simulator decides whether the failure is real.

## What's next

- Noise, slew-rate, PSRR and Monte Carlo mismatch analyses.
- More topologies.
- Running forks as Nebius Serverless Jobs for large corner campaigns.
- Exporting the robustness envelope in a standard datasheet format.

## Pre-existing work

The circuit engine (`bias/`) comes from our earlier project,
[cadence_auto](https://github.com/Amritha902/cadence_auto). Everything in
`tapeout/` and `web/` was built during the submission period:

- the Nemotron/Token Factory agents
- Tavily research
- the compiler repair loop and the sizing race
- the falsification engine, multi-corner repair and regression tests
- the auditor, the provenance labelling and the UI

In `bias/`, the DC-servo testbench and process-corner forking are also new.

## Links

- **Code (public, MIT):** https://github.com/Amritha902/NEBIUS
- **Demo (instant replay):** see "Try it out" links
- **Verify the evidence yourself:** `python -m tapeout.verify web/replays/ppg-offline.jsonl`
