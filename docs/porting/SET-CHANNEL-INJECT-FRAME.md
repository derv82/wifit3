# set_channel and inject_frame: failure handling

Rules for a port's `set_channel` and `inject_frame` failure paths. The C source is the oracle: fail
where the C fails, carry on where the C carries on. Companion to [`METHODOLOGY.md`](METHODOLOGY.md),
[`CODE-STYLE.md`](CODE-STYLE.md), [`GOTCHAS.md`](GOTCHAS.md).

## Contract

- `set_channel(channel, scan=False) -> None`. Returning means tuned (the C's `return 0`).
- Raise `SetChannelError` (`wifit3/errors.py`) where the C's channel-switch path returns a negative error code to mac80211. Message: the C's error / log text.
- A channel not in `SUPPORTED_CHANNELS`: raise `SetChannelError` before any hardware write.
- Device gone: let the `USBError` (ENODEV / LIBUSB_ERROR_NO_DEVICE) propagate; callers test `is_device_gone()`.
- A port that doesn't yet follow the C's retries / log-and-continue (below) lets any other `USBError` propagate unwrapped; callers catch `Exception`.
- `inject_frame(frame_bytes) -> None`. The C TX path is fire-and-forget: on failure, drop the frame, log, return.
- No `bool` returns. No `return False` for "failed".

## Follow the C on every failure path

- Trace the C from the mac80211 op (`config` / channel-switch op, `tx`) down to the USB request. For each step, record: returns error, logs and continues, retries, or ignores.
- Port each step's behaviour as-is:
  - C retries a request N times: retry N times (e.g. Realtek `CONFIG_VENDOR_REQ_RETRY`, mt7601u MCU 5x).
  - C logs and continues: log at the C's level, continue.
  - C ignores the return: ignore it.
  - C returns an error up: raise.
- Most of this lives below `set_channel`: the transport (control requests, bulk-OUT) and the register / MCU / WMI layers. A port follows the C in those layers for every caller (bring-up, RX, TX), not only for `set_channel`.
- C waits for a firmware / MCU reply: wait for it (mt7921/mt7925 channel command, ath9k WMI sequence number check).
- C rejects or remaps an illegal channel: do the same, before any hardware write.
- C skips I/O once the device is removed (Realtek `RTW_CANNOT_RUN`, ath9k `wmi.c`): port the flag.

## State after a failure

- Record the current channel where the C records it; roll back where the C rolls back.
- Never leave a guard that turns the next tune to the same channel into a no-op after a failed tune.
- Restore what the C always restores, even after a mid-tune error: RX queue restart, TX_CONF, etc. (try/finally where the C has no early return).
- TX slots / queue entries: free them on a failed send, as the C does.
- Short writes: treat as the C does (check the URB status / length handling), not as success.

## Logging

- C `*_err` / `pr_err`: `logger.error`.
- C `*_warn` / `WARN`: `logger.warning`.
- Infrequent, user-relevant: `INFO`. Noisier: `DEBUG`. Everything: `TRACE`.

## Tests

- Per chip, under `tests/chips/<chip>/`: fault injection in the mocked USB. Fail request N, assert what the C does (retry count, continue, raise, state restored).
- Cite the C file:line each test follows.
- No cross-driver test for failure behaviour; the drivers differ too much.
- `verify_pcap` captures rarely contain these failures; a passing replay doesn't cover them.
