# Tapeout

**Most AI chip tools give you a green checkmark. Tapeout hires a red team to
break its own datasheet, and publishes what breaks.**

> **Every number on the datasheet links to the exact SPICE deck and ngspice
> log that produced it.** What the red team can't break becomes the promise;
> what breaks becomes a published limit. `python -m tapeout.verify`
> re-simulates all 413 recorded measurements on your own machine.

Describe a product in one sentence ("a heart-rate monitor front-end for a
fitness band") and click **Tape out**. A team of NVIDIA Nemotron agents on
Nebius Token Factory researches the product, plans the silicon, and sizes
every transistor against a real foundry simulator (ngspice, SkyWater 130nm).

Then a second team tries to prove the datasheet wrong. Nemotron reasons about
the circuit, predicts the conditions where each claim should fail, and a
deterministic lab bisects to the exact boundary. A brute-force corner grid
checks the adversary's work. Every claim that breaks inside the operating
envelope goes back to the design team. They re-size the circuit, and the red
team attacks the repaired design again.

What comes out is a datasheet with a **robustness envelope**, a
**falsification log**, and a **shmoo plot**: pass/fail over supply ×
temperature for every process corner, before and after the repair. For every claim it records where the claim holds, where
it breaks, and the simulation deck and ngspice log proving it. You can click
any counterexample on the page.

**Watch:** [demo video, 2 min](submission-kit/video/Tapeout-demo.mp4) ·
[interactive replay](docs/demo/index.html) (open it locally, or via GitHub Pages)

**Read:** [project report (PDF)](docs/Tapeout-Project-Report.pdf) ·
[setup guide (PDF)](docs/Tapeout-Setup-Guide.pdf) ·
[clean-room run](docs/CLEANROOM.md)

---

## What a run looks like

A real offline run (`TAPEOUT_OFFLINE=1`, so textbook hypotheses stand in for
Nemotron's; every simulation is real). The claim is a two-stage op-amp with
≥60 dB gain, ≥2 MHz GBW, ≤200 µW and ≥55° phase margin into 2 pF:

| round | design | what the red team found |
| --- | --- | --- |
| 1 | original sizing; meets spec at typical | **Phase margin broken** at ff (47.4°) and fs (41.8°); **GBW broken** at ss / −40 °C (202 kHz). The corner grid finds PM failing at 23 of 45 corners (worst 23.2° at fs / 1.89 V / 85 °C) and GBW at 19 (worst 76 kHz at sf / 1.71 V / −40 °C). |
| | repair 1 | Re-sized against nominal + the two worst corners, warm-started from the broken design. |
| 2 | first repair | PM and GBW hold at those corners. The grid finds what the hypotheses missed: **gain 59.7 dB** at ff / 1.71 V / 85 °C and **PM 54.6°** at ss / 1.8 V / 85 °C. |
| | repair 2 | Re-sized against nominal + **all four** corners that ever broke a claim. Earlier counterexamples stay as regression tests. |
| 3 | second repair | **Nothing breaks inside the envelope.** Worst cases: gain 60.2 dB, GBW 3.23 MHz, PM 92°. Limits published outside it: GBW holds down to a 1.603 V supply; gain holds up to Vcm = 0.80·VDD. |

252 simulations (180 of them stress runs), about 3 minutes, all on SkyWater
130nm foundry models.

---

## How it works

```
 idea ─▶ SCOUT ──────────▶ ARCHITECT ───────▶ COMPILER ───────▶ FAB ─────────────────────▶ RED TEAM ──────────────────▶ WRITER ─▶ AUDITOR ─▶ datasheet
        Tavily search     Nemotron Ultra      deterministic     Nemotron Super  vs. DE     Nemotron Ultra hypotheses     Nemotron    every number
        + Nemotron Nano   product → blocks    rejects bad       race on ngspice;           + bisection + corner grid     Nano        traced to a
        cited facts       (never a netlist)   plans; Ultra      logic proved by            ↺ repair (Super vs DE,                    simulation,
                                              repairs them      transient sim              multi-corner) ↺ re-attack                 or struck out
```

| agent | model (Token Factory) | job |
| --- | --- | --- |
| **Scout** | Nemotron **Nano** + **Tavily** | Writes search queries and pulls numeric requirements (signal bandwidth, amplitude, power budget) out of the results. A fact whose URL was not actually retrieved is dropped. It also finds a **commercial op-amp** for the same job; that part's figures become a "Commercial ref." column, each kept only if it appears verbatim in the retrieved page. |
| **Architect** | Nemotron **Ultra** | Turns the product into a chip plan: amplifier specs as sentences with units, plus logic cells from a fixed menu of 20. It is told the fab's measured envelope. |
| **Compiler** | deterministic | Parses every spec and checks feasibility. Rejections go back to the model, which repairs its own plan (Super takes over for repair rounds). This is the coding-agent loop, with the spec parser playing the test suite. |
| **Fab** | Nemotron **Super** vs. differential evolution | The two race to size each amplifier, each against its own simulation counter. Nemotron reads measured gain, GBW, PM and power, explains its next move, and proposes sizes. The first to meet spec stops the other. Logic blocks are proved against their full truth table by transient simulation. |
| **Red team** | Nemotron **Ultra** + deterministic lab | Ultra reads the topology, sizing and margins and proposes 4–6 failure hypotheses, each a mechanism plus a stress direction. The lab tests each at its extreme and bisects to the boundary. Then a 45-point corner grid audits the hypotheses. |
| **Repair** | Nemotron **Super** vs. DE | Re-sizes against nominal plus every condition that broke a claim, scored on the worst case. Nemotron starts from the broken design, the way an engineer edits a schematic; DE starts fresh. |
| **Writer + auditor** | Nemotron **Nano** + deterministic | Nano writes the datasheet prose. The auditor extracts every quantity with a unit and every transistor count, and matches each against simulated or specified values. Unsupported figures go back once; any that survive are struck through on the page. |

Why the tiers are split like this: Ultra makes the two decisions where
reasoning quality decides the outcome, the chip plan and the failure
hypotheses. Super runs the inner loop with the simulator (10–20 calls per
block), where latency matters. Nano handles extraction and prose.

### The stress knobs (all real, all in the deck)

| knob | range the adversary may use | operating envelope (the promise) |
| --- | --- | --- |
| process corner | tt, ss, ff, sf, fs (sky130 foundry corner models) | all five |
| supply | 1.5–2.0 V | 1.71–1.89 V |
| temperature | −55–150 °C | −40–85 °C |
| input common mode | 0.1–0.9 · VDD | 0.4–0.6 · VDD |
| load capacitance | 1–10× nominal | nominal |

A break **inside** the envelope is a design bug: the design team must repair
it. A break **outside** is a limit, published with its boundary and the run
that proves it.

### Every stress run is a fork

The sized design is frozen and content-hashed. Each stress condition renders
its own deck from that frozen sizing and runs in its own ngspice process. A
failed run becomes a leaf in the log; it never mutates the design. Each run
records the condition, the measured values, the exact deck, the ngspice
output and the design hash, and the datasheet links to them.

### Why it does not hallucinate

No model writes a netlist. Models choose blocks from a menu, propose numbers
and hypotheses, and write prose. Synthesis is deterministic, ngspice is the
only source of truth, the compiler gates the plan, and the auditor gates the
prose. Hypotheses are clipped to the legal knob ranges, and a claimed failure
counts only if a simulation shows it.

### A bug found while building this

The engine this grew from measured amplifiers open-loop with no DC feedback.
Under that testbench, a 5% supply droop or 85 °C rails the output and gain
"collapses" from 63 dB to 28 dB. A falsifier built on it would have published
dozens of failures that a real, fed-back part never shows.

The fix is the standard one: a DC servo, a 1 GH inductor into a 1 F cap that
feeds the output back to the inverting input. It pins the operating point and
keeps the AC measurement open-loop (`Testbench(servo=True)`). It also showed
that the two-stage op-amp's real gain ceiling on sky130 is about 82 dB, not
the 65 dB the railed testbench suggested.

Two more bugs came out of the arms race itself:

- **Repairs targeted the wrong corners.** The first version took its repair
  conditions from the first few grid failures, not the worst ones. It now
  repairs against each broken claim's worst in-envelope run.
- **A repair reopened an earlier failure.** Repair 2 sized against round 2's
  corners only, and GBW broke again at the round-1 corner. Every
  counterexample is now a regression test that every later repair must pass.

Repairs also start from the design that broke: the differential-evolution
population is seeded with it plus jittered copies. Measured: from a random
start, a two-stage op-amp needs about 480 candidates to hold at nominal plus
its worst corners, and 2 × 300 was not enough.

---

## Run it

Needs Python 3.11+ and ngspice.

```bash
git clone https://github.com/Amritha902/NEBIUS.git && cd NEBIUS
```

```bash
sudo apt install ngspice        # macOS: brew install ngspice
```

```bash
scripts/fetch_pdk.sh sky130     # the two sky130 primitives, all corners (~2 MB)
```

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
cp .env.example .env            # add NEBIUS_API_KEY (and TAVILY_API_KEY)
```

```bash
set -a; . ./.env; set +a; .venv/bin/uvicorn web.server:app --port 8010
```

No keys? `python -m tapeout.check --offline` proves ngspice, the sky130
corner models and a real corner simulation in about 2 seconds.

Before a demo, prove the live path in about 30 seconds. This lists the models
your key can see, resolves the three tiers, makes one call to each, checks
Tavily, and exits non-zero on any failure:

```bash
.venv/bin/python -m tapeout.check
```

Open <http://localhost:8010>, pick an example, and click **Tape out**. Or from
the terminal:

```bash
.venv/bin/tapeout "a wake-on-sound smart doorbell" --json run.json
```

**Static demo, no server at all.** This builds a self-contained replay page
for GitHub Pages: no cold start, no credits, labelled `REPLAY`.

```bash
python scripts/export_static.py
```

Enable it under Settings → Pages → Deploy from branch `main`, folder `/docs`.
It then serves at `https://<user>.github.io/NEBIUS/demo/`.

**Instant replay for visitors.** A full run takes 3–5 minutes and spends
model credits. Record one run, and the hosted demo offers
"Watch a recorded run in ~40 s". It replays the exact event stream,
time-compressed and labelled `REPLAY`, with every trace still clickable. Live
recordings are listed before offline ones:

```bash
.venv/bin/tapeout "a heart-rate (PPG) monitor front-end for a fitness band" --record web/replays/ppg-live.jsonl
```

**Nothing falls back silently.** A banner reads `LIVE NEMOTRON · ACTIVE`, or
names each fallback and its reason. Every agent card is tagged
`LIVE NEMOTRON` or `FALLBACK`, and the datasheet carries a provenance table
listing which model produced what.

Without `NEBIUS_API_KEY` it runs in **offline mode**: a reference plan and
textbook failure mechanisms stand in for the agents, and every simulation is
still real. Model ids are discovered from Token Factory's `/v1/models` at
startup. Pin them with `TAPEOUT_MODEL_ULTRA`, `_SUPER` and `_NANO`.

### Deploy (one container)

```bash
docker build -t tapeout .
```

```bash
docker run -p 8010:8010 -e NEBIUS_API_KEY -e TAVILY_API_KEY tapeout
```

The image fetches only the sky130 primitives it uses. The build fails unless a
half adder is proved and a slow-corner, 85 °C fork simulates inside the image.
CI builds the image and smoke-tests the running container on every push.
The same image runs on any container host. `render.yaml` is included, and on
Nebius you can serve it as a Serverless Endpoint.

### Check the evidence yourself

Every deck a datasheet links to is stored in the recording. This re-runs all
of them on your own ngspice and sky130 models, then compares every recorded
measurement:

```bash
.venv/bin/python -m tapeout.verify web/replays/ppg-offline.jsonl
```

```
VERIFIED: all 83 runs reproduced; 413 recorded measurements matched within 1e-03 relative.
```

It trusts nothing from the run except the deck text. A doctored number or a
doctored deck makes it fail (`tests/test_verify.py`). CI runs it on GitHub's
machines on every push.

### Testing the live path without a key

`tests/mock_tokenfactory.py` speaks Token Factory's OpenAI-compatible
dialect: `/v1/models`, streamed `reasoning_content` deltas, inline `<think>`
tags split across chunks, a final usage chunk with empty `choices`, and
`[DONE]`. It can also inject a dropped connection or a malformed chunk.
Every agent runs against it in `tests/test_live_path.py`, so the only part
not under test is the remote server.

```bash
python tests/mock_tokenfactory.py 8765 &
```

```bash
NEBIUS_API_KEY=test-key NEBIUS_BASE_URL=http://127.0.0.1:8765/v1 .venv/bin/tapeout "a PPG front-end"
```

Its first run found three bugs that offline mode could never reach:

- **A `<think>` tag split across stream chunks swallowed the answer.** The
  architect then silently fell back to the reference plan, so a live run
  would have looked like offline mode.
- **The sizing lane looped without bound.** Re-proposing an already-simulated
  sizing costs no simulation budget, so the lane spent 8,884 calls (about
  $1.17) in one run. It is now capped at twice its simulation budget.
- **An empty datasheet reply counted as success.**

A dropped stream is now an error that gets retried, never a short answer.

### Tests

A recorded clean-room run (fresh clone, fresh venv, no keys) is in
[docs/CLEANROOM.md](docs/CLEANROOM.md).

```bash
TAPEOUT_OFFLINE=1 .venv/bin/pytest -q     # 346 tests: engine, compiler, auditor, falsifier, servo, live path vs. mock
```

---

## Layout

```
tapeout/
  llm.py        Token Factory client: streaming, <think>/reasoning capture, tier discovery, usage ledger
  check.py      `python -m tapeout.check`: proves the live path with a real key
  research.py   scout: Tavily + Nemotron Nano, citations checked against retrieved URLs
  architect.py  Nemotron Ultra planner + the deterministic compiler + self-repair loop
  fab.py        Nemotron Super vs. DE sizing race; multi-condition repair; logic proofs
  falsify.py    red team: hypotheses, forks, bisection, corner grid, robustness envelope
  writer.py     datasheet prose + numeric audit
  pipeline.py   the one-click orchestration, as an event stream
web/            FastAPI server-sent events + a single-page UI
bias/           the circuit engine: synthesis, netlist validation, topologies, ngspice
```

## Honest limits

- **Two amplifier topologies** (5T OTA, two-stage Miller) and 20 static-CMOS
  logic cells. The architect works inside that menu and is told so.
- **No noise, slew-rate, PSRR or mismatch analysis yet.** The red team attacks
  what the testbench measures: gain, GBW, phase margin and power. Monte Carlo
  mismatch needs the sky130 statistical parameters driven from a distribution,
  which the nominal shim zeroes.
- **The grid samples the envelope** (5 corners × 3 supplies × 3 temperatures).
  "Holds" means it held at every sampled point and every adversarial probe.
  That is strong evidence, not a proof.
- **The live path is tested against a protocol-faithful mock, not yet against
  Token Factory itself.** Run `python -m tapeout.check` with a real key first.
- Forks are isolated ngspice processes in one container. Running them as
  Nebius Serverless Jobs, or in sandboxes for very large campaigns, is the
  natural next step and is not wired up yet.

## Built on earlier work

The circuit engine in `bias/` comes from the author's earlier project
[cadence_auto](https://github.com/Amritha902/cadence_auto): text-to-circuit
synthesis, netlist validation and an LLM-sizing benchmark on open PDKs.
Everything in `tapeout/` and `web/` is new in this project:

- the Nemotron/Token Factory agents
- Tavily research
- the architect and compiler repair loop
- the Nemotron-vs-evolution race
- the whole falsification engine: hypotheses, forks, bisection, grid,
  multi-corner repair, robustness envelope and falsification log
- the auditor and the UI

In `bias/`, the DC-servo testbench, process-corner forking and the
recalibrated gain ceilings are also new.

## Notes from building on Token Factory and Nemotron

- **OpenAI-compatible streaming made the live UI cheap to build.** The agent
  console is just the SSE stream re-emitted. Usage arrives in the final chunk
  with `stream_options.include_usage`, which made per-tier cost accounting
  trivial.
- **Thinking output varies by model**, between a `reasoning_content` delta and
  inline `<think>` tags. The client handles both. A documented, uniform field
  across the Nemotron family would remove that code.
- **Model ids are case-sensitive**, so pinned and default ids are matched
  case-insensitively against `/v1/models` and the exact listed id is used.
- **Model-id discovery** via `/v1/models` was essential; ids for the Nemotron 3
  family were hard to confirm from public docs alone. A stable alias per tier
  (`nemotron-ultra-latest`) would help.
- **Tier splitting works as advertised.** Ultra's hypotheses were the part
  where reasoning depth changed the outcome. The sizing loop needs fast, short
  JSON replies, so it runs on Super with thinking disabled through
  `chat_template_kwargs`.

## Author

Built by **Amritha S** ([@Amritha902](https://github.com/Amritha902)).

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The SkyWater sky130 models are Apache-2.0. They
are fetched at build time, not vendored.
