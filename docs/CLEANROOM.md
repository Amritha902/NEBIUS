# Clean-room verification

A fresh clone from GitHub into an empty directory, a new virtualenv, and
exactly the README's commands, recorded as run. No API keys were set. This
verifies the simulation path and the 340-test suite. The live Token Factory
path needs a key; check it with `python -m tapeout.check`.

- **Date:** 2026-10-29 15:49 UTC
- **Commit:** a07f5d8
- **OS:** Linux 6.18.44-fc-v185
- **Python:** 3.13.17
- **Simulator:** ngspice-42. It was already installed system-wide on this machine; on a
  bare machine, install it first with `apt install ngspice` (or
  `brew install ngspice`). If it is missing, the check says so.

```
$ git clone https://github.com/Amritha902/NEBIUS.git
commit a07f5d8
$ scripts/fetch_pdk.sh sky130
Verify with:  .venv/bin/bias list
$ python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
installed
$ .venv/bin/python -m tapeout.check --offline
Simulator
  [ ok ] ngspice                            /usr/bin/ngspice
  [ ok ] sky130 models                      tt/ss/ff/sf/fs corners present
  [ ok ] corner simulation (ss/1.71V/85C)   gain 41.5 dB in 0.2s

SIMULATION PATH READY
$ TAPEOUT_OFFLINE=1 .venv/bin/pytest -q
340 passed, 3 warnings in 70.24s (0:01:10)
elapsed 118s
```

To reproduce:

```bash
git clone https://github.com/Amritha902/NEBIUS.git && cd NEBIUS
scripts/fetch_pdk.sh sky130
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m tapeout.check --offline
TAPEOUT_OFFLINE=1 .venv/bin/pytest -q
```
