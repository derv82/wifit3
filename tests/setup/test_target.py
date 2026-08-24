"""Linux replug classification: the safe default is replug-required; only chips that genuinely
self-cold in userland opt out. Exercised through target_for_vidpid (the exact call the splash
uses) with each driver's own first VID:PID so the test never hardcodes brittle id literals."""
import importlib

from wifit3.setup import target_for_vidpid
from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
from wifit3.chips.mt76x0u.driver import MT76x0UDriver
from wifit3.chips.mt76x2u.driver import MT76x2UDriver
from wifit3.chips.mt7921au.driver import MT7921AUDriver
from wifit3.chips.rt5372.driver import RT5372Driver


def _first_id(cls):
    pkg = importlib.import_module(cls.__module__.rsplit(".", 1)[0])
    e = pkg.SUPPORTED_IDS[0]
    return e.vid, e.pid


def test_self_cold_chips_auto_connect():
    for cls in (MT76x0UDriver, MT76x2UDriver):
        v, p = _first_id(cls)
        t = target_for_vidpid(v, p)
        assert t is not None and t.replug_after_modprobe is False, cls.__name__


def test_replug_required_by_default_and_when_explicit():
    # RT5372 sets nothing → picks up the safe default; mt7921au and ar9271 set it explicitly.
    for cls in (RT5372Driver, MT7921AUDriver, AR9271V2Driver):
        v, p = _first_id(cls)
        t = target_for_vidpid(v, p)
        assert t is not None and t.replug_after_modprobe is True, cls.__name__
