# Devpost submission: copy-paste text

**Project name:** Tapeout

**Tagline:** An AI chip designer that tries to prove its own datasheet wrong,
and publishes where it breaks.

**Track:** Best Apps and Agents. Also enter: Best Use of Tavily.

**Built with:** nvidia-nemotron, nebius-token-factory, tavily, python,
fastapi, ngspice, skywater-sky130, server-sent-events, docker

---

## Inspiration

LLM agents can now draft analog circuits, but a generated datasheet is a
claim, not evidence. Every demo shows a green checkmark at one operating
point: typical silicon, nominal supply, room temperature. Real chips get
judged at their corners. We wanted the agent that does the engineer's
hardest job: trying to break the claim before anyone else does.

## What it does

Type a product ("a heart-rate monitor front-end for a fitness band") and
click once. The run then goes through six stages:

1. **Scout** (Tavily + Nemotron Nano) researches the product and keeps only
   facts whose source URL it actually retrieved.
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

The result is a datasheet with a **robustness envelope** and a
**falsification log**. Click any number to see the exact SPICE deck and raw
ngspice output behind it.

## How we built it

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

- **The original testbench lied.** It measured amplifiers open-loop with no
  DC feedback, so a 5% supply droop "collapsed" gain from 63 dB to 28 dB. The
  falsifier would have published fake failures. A DC servo fixed it, and the
  real sky130 gain ceiling turned out to be about 82 dB, not 65.
- **The arms race found our own bugs.** One repair targeted the wrong corners.
  A later repair reopened a failure fixed one round earlier, and the re-attack
  caught it. That is why counterexamples are now permanent regression tests.
- **Streaming edge cases.** A protocol-faithful Token Factory mock found that
  `<think>` tags split across chunks swallowed the architect's answer. It
  also found a sizing loop that could spend thousands of calls on repeated
  proposals. Both are fixed and covered by tests.

## Accomplishments that we're proud of

- A datasheet whose limits are found by simulation, with every number
  traceable to the run that produced it.
- No silent fallbacks: if Nemotron didn't do something, the page says so.
- 331 tests, CI that builds the container and proves a sky130 simulation
  inside it.

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

## Feedback on Nebius Token Factory and NVIDIA Nemotron

See the "Feedback" section of the README.
