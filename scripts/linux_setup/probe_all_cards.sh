#!/usr/bin/env bash
# Guided L1/L2 capture loop for a Kali session — DEVICE-SETUP.md.
#
# Plug in one card, press Enter; it probes that card (non-root kernel detach = L1, per-module
# detach behaviour = L2) and dumps the full USB picture, appending everything to ONE transcript.
# Ctrl+C (or Ctrl+D) when you've cycled through your cards. Hand back results/session-<stamp>.txt
# and that's the whole picture — one file.
#
# Run AS YOUR NORMAL USER (not sudo): root makes the "non-root detach" result meaningless.
# Only the one-time udev-rule install elevates, via pkexec (a graphical password prompt).
#
#   bash scripts/linux_setup/probe_all_cards.sh
#
# Each card is passive: detach only unbinds the kernel driver (scoped rmmod), never TXes; a
# replug re-attaches it. Unplug the previous card before plugging the next to keep rows clean.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PROBE="$HERE/probe_l1_l2.py"
SEP="------------------------------------------------------------------------------"

# The project venv carries PyUSB + the live driver registry; fall back to system python3.
if [ -x "$ROOT/.venv/bin/python3" ]; then
  PY="$ROOT/.venv/bin/python3"
else
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "No python3 found. Run from the repo with the project venv set up (.venv/bin/python3)."
  exit 2
fi
if [ ! -f "$PROBE" ]; then
  echo "Can't find probe_l1_l2.py next to this script ($PROBE)."
  exit 2
fi

RESULTS="$HERE/results"
mkdir -p "$RESULTS"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$RESULTS/session-$STAMP.txt"
TSV="$RESULTS/results-$STAMP.tsv"

# tee to both the terminal and the transcript. log() appends; the header seeds the file.
log() { printf '%s\n' "$*" | tee -a "$LOG"; }

{
  echo "wifit3 L1/L2 capture session  $STAMP"
  echo "user   : $(id -un) (uid $(id -u))"
  echo "host   : $(uname -srm)"
  echo "python : $PY"
} | tee "$LOG"

if [ "$(id -u)" -eq 0 ]; then
  log ""
  log "[!!] Running as ROOT — L1 (non-root detach) will read N/A for every card."
  log "     Quit (Ctrl+C) and rerun as your normal user; only the rule install needs elevation."
fi

# One lsusb snapshot up front (gold for cross-referencing later). Into the file, not the screen.
{
  echo
  echo "== lsusb =="
  lsusb 2>&1
  echo
  echo "== lsusb -t =="
  lsusb -t 2>&1
} >>"$LOG"
log ""
log "(captured an lsusb snapshot into the transcript)"

# One-time permissive udev rule. Installing it is the single pkexec prompt; afterwards a
# non-root probe can open + detach. Skip if you already installed it this boot.
printf '\nInstall/refresh the udev access rule now (one pkexec prompt)? [Y/n] '
read -r ans || ans="n"
case "${ans:-Y}" in
  [Nn]*)
    log "[*] Skipped rule install — assuming it's already in place."
    ;;
  *)
    log "[*] Installing udev rule (perms: uaccess+plugdev)..."
    "$PY" "$PROBE" --install-rule --perms all 2>&1 | tee -a "$LOG"
    log "[*] If it installed OK: UNPLUG the card now — you'll replug it as card #1 below."
    ;;
esac

CARDS=0
DONE=0
trap 'DONE=1' INT

log ""
log "$SEP"
log "LOOP: unplug the previous card, plug in the NEXT one, then press Enter."
log "      You may type a label first (e.g. PAU06) to tag it. Ctrl+C when finished."
log "$SEP"

while [ "$DONE" -eq 0 ]; do
  printf '\n>>> Next card — plug in, optional label then Enter (Ctrl+C to finish): '
  if ! read -r LABEL; then DONE=1; fi
  [ "$DONE" -eq 1 ] && break
  CARDS=$((CARDS + 1))

  log ""
  log "$SEP"
  log "CARD #$CARDS   $(date +%H:%M:%S)${LABEL:+   label: $LABEL}"
  log "$SEP"

  # L1/L2 probe of whatever supported card is present (prints the verdict, appends a TSV row).
  "$PY" "$PROBE" --out "$TSV" 2>&1 | tee -a "$LOG"

  # Full USB list too, so an unknown / not-yet-ported card is still captured (VID:PID + driver).
  log ""
  log "-- full USB list (* = already in the wifit3 registry) --"
  "$PY" "$PROBE" --list-all 2>&1 | tee -a "$LOG"

  log ""
  log "[ok] card #$CARDS recorded."
done

trap - INT

log ""
log "$SEP"
log "Done — probed $CARDS card(s)."
log "Transcript : $LOG"
log "TSV summary: $TSV"
log "$SEP"
log ""
log "-- accumulated L2 table --"
"$PY" "$PROBE" --show --out "$TSV" 2>&1 | tee -a "$LOG"
log ""
log "Send back $LOG (the .tsv is a bonus) — that's everything I need for the L1/L2 picture."
