#!/usr/bin/env bash
# Guided non-root-detach capture loop for a Kali session.
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

# Elevation method for the one-time rule install. Two ways to get root:
#   sudo    asks for your password right here in the terminal — works from ANY terminal,
#           including a text console (tty), since it needs no desktop.
#   pkexec  pops a graphical polkit dialog, but ONLY if a polkit authentication agent is
#           registered to THIS login session. Run from a tty (or any session whose desktop
#           agent isn't reachable) it blocks forever with no dialog — the classic silent
#           freeze. The graphical dialog can also open unfocused behind the terminal.
# This script is always interactive (it reads from the terminal), so a controlling tty is
# present and sudo's in-terminal prompt is the reliable default. Force a method with
# WIFIT3_ELEVATE=sudo|pkexec.
case "${WIFIT3_ELEVATE:-auto}" in
  sudo)   ELEVATE_FLAG="--use-sudo" ;;
  pkexec) ELEVATE_FLAG="" ;;                       # empty -> probe_l1_l2.py defaults to pkexec
  *)      if [ -t 0 ] && command -v sudo >/dev/null 2>&1; then
            ELEVATE_FLAG="--use-sudo"
          else
            ELEVATE_FLAG=""
          fi ;;
esac

# One-time permissive udev rule. Installing it is the single password prompt; afterwards a
# non-root probe can open + detach. Skip if you already installed it this boot.
printf '\nInstall/refresh the udev access rule now (one password prompt)? [Y/n] '
read -r ans || ans="n"
case "${ans:-Y}" in
  [Nn]*)
    log "[*] Skipped rule install — assuming it's already in place."
    ;;
  *)
    if [ "$ELEVATE_FLAG" = "--use-sudo" ]; then
      log "[*] Installing udev rule via sudo (enter your password below if prompted)..."
    else
      log "[*] Installing udev rule via pkexec (a graphical password dialog should pop)..."
    fi
    "$PY" "$PROBE" --install-rule --perms all $ELEVATE_FLAG 2>&1 | tee -a "$LOG"
    log "[*] If it installed OK: UNPLUG the card now — you'll replug it as card #1 below."
    ;;
esac

CARDS=0

# End-of-run summary. Called from BOTH the Ctrl+C trap and a Ctrl+D (EOF) on the prompt, so
# either way of finishing prints the table and exits. It must live in a function because a
# bash `read` with a trap installed RESTARTS after the handler returns instead of returning —
# so a "set a DONE flag and break" loop never breaks on Ctrl+C. Exiting from inside the trap
# is the only thing that reliably stops it. `trap - INT` first so a second Ctrl+C (mash) falls
# through to the default and hard-kills, rather than re-entering this.
finish() {
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
  exit 0
}
trap 'echo; finish' INT

log ""
log "$SEP"
log "LOOP: unplug the previous card, plug in the NEXT one, then press Enter."
log "      You may type a label first (e.g. PAU06) to tag it. Ctrl+C (or Ctrl+D) when finished."
log "$SEP"

while true; do
  printf '\n>>> Next card — plug in, optional label then Enter (Ctrl+C/Ctrl+D to finish): '
  read -r LABEL || finish   # EOF (Ctrl+D) finishes; Ctrl+C finishes via the trap above
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
