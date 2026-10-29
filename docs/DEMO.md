# Three-minute demo script

Record at 1080p with the browser at ~110% zoom. Run `python -m tapeout.check`
first, and record only after it prints `LIVE PATH READY`. Keep a recording of
a clean full run as a backup.

**0:00–0:20: the trust problem.**
"LLM agents can draft circuits now. The hard part is believing the datasheet.
Every demo you'll see shows a circuit that passes. This one shows you where
the circuit breaks, and proves it with a simulation log."

**0:20–0:50: one click.**
Click the "heart-rate (PPG) monitor" example. Point at the green banner,
`LIVE NEMOTRON · ACTIVE`, and the tags on each agent card. Narrate as it runs:

- the scout's Tavily facts
- Ultra's plan, and a compiler rejection being repaired if one shows
- the race chart: Nemotron's reasoning line against the evolutionary search

**0:50–1:40: the red team breaks it.**
Read one hypothesis card aloud: a mechanism, then red probe squares. From the
offline reference run (a live run will differ):

- **Round 1:** phase margin fails at ff (47.4°); the grid finds 23.2° at
  fs / 1.89 V / 85 °C; GBW collapses to 76 kHz at sf / 1.71 V / −40 °C.
- **Repair 1, then round 2:** the grid catches what the hypotheses missed:
  gain 59.7 dB and PM 54.6°.
- **Repair 2:** every earlier counterexample is now a regression test.
- **Round 3:** nothing left to break inside the envelope.

**1:40–2:20: the artifact.**
Scroll to the Robustness Envelope table and click a run id. The modal shows
the exact SPICE deck (corner include, supply, the servo loop) and the raw
ngspice output. This is the key shot; rehearse it until it's automatic. Then:

- the Falsification Log: rounds, verdicts, repairs
- the Provenance table: which model produced what

**2:20–3:00: why it matters.**
Tell the testbench story: open-loop decks made a 5% supply droop look like
63 → 28 dB, so the falsifier would have published fake failures. A DC servo
fixed it, and the real gain ceiling turned out to be ~82 dB.

Close on the datasheet: "Limits proven, not claimed. Every number re-runs from
one command."
