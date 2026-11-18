"""linux/iopoll.h for RTL8922AU register polls."""
import time


def read_poll_timeout(op, cond, sleep_us: int, timeout_us: int) -> tuple[bool, int]:
    """linux/iopoll.h read_poll_timeout: op() every sleep_us until cond(value) or timeout_us of
    wall time has passed, then one last op(). Returns (cond held, last value)."""
    deadline = time.perf_counter() + timeout_us / 1_000_000
    while True:
        val = op()
        if cond(val):
            return True, val
        if time.perf_counter() > deadline:
            val = op()
            return cond(val), val
        time.sleep(sleep_us / 1_000_000)


def read_poll_timeout_atomic(op, cond, sleep_us: int, timeout_us: int,
                             sleep_before_read: bool = False) -> tuple[bool, int]:
    """linux/iopoll.h poll_timeout_us_atomic: no clock; each try spends sleep_us plus 1 ns of the
    timeout_us budget. Returns (cond held, last value)."""
    left_ns = timeout_us * 1000
    if sleep_before_read and sleep_us:
        time.sleep(sleep_us / 1_000_000)
        if timeout_us:
            left_ns -= sleep_us * 1000
    while True:
        expired = timeout_us and left_ns < 0
        val = op()
        if cond(val):
            return True, val
        if expired:
            return False, val
        if sleep_us:
            time.sleep(sleep_us / 1_000_000)
            if timeout_us:
                left_ns -= sleep_us * 1000
        if timeout_us:
            left_ns -= 1
