# Three-minute demo script

**Before recording**
- Run `python -m tapeout.check` and record only after it prints `LIVE PATH READY`.
- Record at 1080p with the browser at ~110% zoom, with audio.
- Say "NVIDIA Nemotron" and "Nebius Token Factory" out loud at least twice
  each.
- Upload to YouTube early: processing can take hours.
- Keep a clean recorded run as a backup. The static replay page
  (`docs/demo/`) works with no server at all.

| time | on screen | say |
| --- | --- | --- |
| 0:00–0:15 | Title page | "Every AI chip demo shows you a circuit that passes. Tapeout shows you where it breaks, and proves it with a simulation log." |
| 0:15–0:40 | Click the PPG example. Show the green `LIVE NEMOTRON · ACTIVE` banner and the model ids on the agent cards. | "Six agents on **NVIDIA Nemotron**, served by **Nebius Token Factory**: Ultra plans and attacks, Super sizes, Nano researches and writes. Tavily grounds it in real datasheets." |
| 0:40–1:10 | Scout's commercial-reference line; the race chart with Nemotron's reasoning under it. | "It found the op-amp a product team would buy, kept only the numbers it could find verbatim in that datasheet, and is now sizing ours on SkyWater 130nm." |
| 1:10–1:40 | Red-team cards turning red: phase margin and bandwidth broken at the corners. | "Nemotron predicts where each claim should fail. The simulator proves it, and a corner grid checks the predictions." |
| 1:40–2:05 | The shmoo plot: before (red inside the dashed box) vs. after repair (green). Click one red cell to open its deck and ngspice log. | "Each cell is a real simulation. Red inside the box was a broken promise. After repair it's green, and the limits outside the box are published, not hidden." |
| 2:05–2:25 | The **provenance table**. Hold it for at least 8 seconds. | "Every agent declares whether it ran live on Nemotron. Nothing falls back silently." |
| 2:25–2:50 | The robustness table and falsification log. Click a number to open its trace. | "One bug it caught was in our own testbench: a 5% supply droop looked like 63 dB collapsing to 28. A DC servo fixed it." |
| 2:50–3:00 | End card: repo URL, demo URL, MIT. | "Limits proven, not claimed. **NVIDIA Nemotron** on **Nebius Token Factory**." |

A live run gives different numbers from any earlier run. Narrate what's on
screen; don't read numbers from this script.
