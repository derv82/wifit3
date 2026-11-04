import pytest

from wifit3.chips.ar9271_v2.transport import AR9271Transport


@pytest.fixture(autouse=True)
def waiter_reads_reg_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """No REG_IN reader thread here: the waiting thread reads REG_IN and hands each frame to rx_msg."""
    def wait_for_completion_timeout(self: AR9271Transport, completion, timeout: float) -> bool:
        while not completion.is_set():
            self.htc.rx_msg(self.reg_in())
        return True

    monkeypatch.setattr(AR9271Transport, "wait_for_completion_timeout", wait_for_completion_timeout)
