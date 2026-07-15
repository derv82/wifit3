# scripts/

Two kinds of tooling, kept separate on purpose:

- **`scripts/<chip>/`, `scripts/diag/`, `scripts/wep/`, `pcap_slicer.py`, ...** — one-off dev / RE /
  debug / hardware-test scripts. They import from `wifit3`; nothing in `src/` imports them, which
  keeps the installed wheel product-only. Don't move these into `src/`.
- **`src/wifit3/scripts/capture.py`** — the one script that lives *inside* the package, because it's
  a durable product tool: it produces the cold-boot pcaps + `main.log` the whole RE workflow
  consumes, so it ships and versions with wifit3. The dividing line is "durable product tool" vs
  "throwaway dev artifact."
