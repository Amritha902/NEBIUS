# Tapeout: one container with the agents, the web UI, ngspice and the
# SkyWater 130nm device models. Every page that shows a number ran a real
# simulation inside this image.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ngspice git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Only the two sky130 primitives the circuits use (~2MB, all corners), not
# the 770MB library. Before the source copy so code edits don't re-fetch.
COPY scripts ./scripts
COPY pdks ./pdks
RUN chmod +x scripts/fetch_pdk.sh && scripts/fetch_pdk.sh sky130 \
 && rm -rf pdks/sky130/.git

COPY pyproject.toml README.md ./
COPY bias ./bias
COPY tapeout ./tapeout
COPY web ./web
RUN pip install --no-cache-dir -e .

# Fail the build rather than ship an image whose simulator is broken: prove a
# logic cell and fork one corner run.
RUN python -c "\
from bias import pdk, verify;\
assert 'sky130' in pdk.available(), pdk.available();\
r = verify.verify('half_adder', pdk.get('sky130')); assert r.passed, r.reason;\
from bias import topology as T, specparse; from tapeout import falsify;\
t = T.get('ota5t'); s = falsify.Snapshot(t, t.defaults(), pdk.get('sky130'), 1e-12, specparse.parse('40dB gain').spec);\
run = s.simulate(falsify.Condition('ss', 1.71, 85.0, 1e-12, 0.5)); assert run.ok, run.log;\
print('sky130 verified in image')"

ENV PORT=8010
EXPOSE 8010
CMD ["sh", "-c", "uvicorn web.server:app --host 0.0.0.0 --port ${PORT}"]
