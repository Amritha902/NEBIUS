# Three-minute demo script

**Before recording**
- Run `python -m tapeout.check` and record only after it prints `LIVE PATH READY`.
- Record at 1080p with the browser at ~110% zoom, with audio.
- Say "NVIDIA Nemotron" and "Nebius Token Factory" out loud at least twice
  each.
- Upload to YouTube early: processing can take hours.
- Keep a clean recorded run as a backup. The static replay page
  (`docs/demo/`) works with no server at all.

Rule: models talk at most 30% of the time; simulator evidence fills the
rest.

| time | on screen | say |
| --- | --- | --- |
| 0:00–0:20 | One-line definition, then the shmoo plot: red before, green after | "AI can write a datasheet. What's rare is one that dares to be proven wrong. Tapeout writes a datasheet, then sends a red team to prove where it's false." |
| 0:20–0:50 | Architecture: Scout → Architect → Fab → Red team → Repair → Datasheet | "Three **NVIDIA Nemotron** tiers on **Nebius Token Factory**: Ultra for hypotheses, Super for sizing, Nano for prose. But the judge is not a model. The judge is ngspice." |
| 0:50–1:40 | Click a run id; show the SPICE deck and ngspice log | "The red team predicted phase margin would collapse at this corner. That isn't the model talking: it's the simulator, on this exact deck. Every number links back here." |
| 1:40–2:20 | The shmoo plot, before vs. after | "After two repairs, 42 failures inside the envelope became zero. The red cells left are outside it. They aren't bugs; they're published limits." |
| 2:20–2:50 | The provenance table, held at least 8 seconds, LIVE/FALLBACK tags visible | "This table declares what live **Nemotron** did and what a fallback did. Nothing is passed off as live." |
| 2:50–3:00 | Repo and demo URL | "Limits proven, not claimed. **NVIDIA Nemotron** on **Nebius Token Factory**." |

A live run gives different numbers from any earlier run. Narrate what's on
screen; don't read numbers from this script.
