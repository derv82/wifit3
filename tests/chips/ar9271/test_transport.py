"""HIF stream reassembly tests for AR9271USBTransport._handle_bulk_in.

These exercise the kernel-faithful parser without touching real USB — the
transport is constructed with a MagicMock device, and we feed bytes directly
into the bulk-in handler while capturing dispatched HTC payloads.
"""
from unittest.mock import MagicMock

import pytest
import usb.core

from wifit3.chips.ar9271.constants import (
    ATH_USB_RX_STREAM_MODE_TAG,
    USB_EP_DATA_WMI_IN,
)
from wifit3.chips.ar9271.transport import AR9271USBTransport


def _hif_chunk(htc_frame: bytes) -> bytes:
    """Wrap one HTC frame in a HIF header + 4-byte padding."""
    pkt_len = len(htc_frame)
    pad = (4 - (pkt_len & 0x3)) & 0x3
    hdr = pkt_len.to_bytes(2, "little") + ATH_USB_RX_STREAM_MODE_TAG.to_bytes(2, "little")
    return hdr + htc_frame + b"\x00" * pad


def _htc_frame(ep: int, payload: bytes, flags: int = 0) -> bytes:
    """Build a minimal HTC frame: 8-B header + payload."""
    p_len = len(payload)
    ctrl = b"\x00\x00\x00\x00"
    return bytes([ep, flags]) + p_len.to_bytes(2, "big") + ctrl + payload


@pytest.fixture
def transport_with_capture():
    """Return (transport, captured_list) — captures payloads dispatched to EP 1."""
    dev = MagicMock(spec=usb.core.Device)
    t = AR9271USBTransport(dev)
    captured: list[bytes] = []
    t.subscribe(1, lambda p: captured.append(bytes(p)))
    return t, captured


async def test_single_frame_in_one_urb(transport_with_capture):
    t, captured = transport_with_capture
    frame = _htc_frame(ep=1, payload=b"hello")
    await t._handle_bulk_in(_hif_chunk(frame))
    assert captured == [b"hello"]
    assert t._rx_buf == b""


async def test_two_bundled_frames_in_one_urb(transport_with_capture):
    """Two short HTC frames sharing a single URB — the historical corruption case."""
    t, captured = transport_with_capture
    urb = _hif_chunk(_htc_frame(1, b"first")) + _hif_chunk(_htc_frame(1, b"second-payload"))
    await t._handle_bulk_in(urb)
    assert captured == [b"first", b"second-payload"]
    assert t._rx_buf == b""


async def test_frame_split_across_urbs(transport_with_capture):
    """A HIF chunk that arrives in two pieces must reassemble across URBs."""
    t, captured = transport_with_capture
    full = _hif_chunk(_htc_frame(1, b"this is a payload spanning two URBs"))
    split = len(full) // 2

    await t._handle_bulk_in(full[:split])
    assert captured == []                  # nothing dispatched yet
    assert len(t._rx_buf) == split          # bytes held for stitching

    await t._handle_bulk_in(full[split:])
    assert captured == [b"this is a payload spanning two URBs"]
    assert t._rx_buf == b""


async def test_split_at_hif_header_boundary(transport_with_capture):
    """URB ends with only 2 of the 4 HIF header bytes — must wait for the rest."""
    t, captured = transport_with_capture
    full = _hif_chunk(_htc_frame(1, b"tiny"))

    await t._handle_bulk_in(full[:2])
    assert captured == []

    await t._handle_bulk_in(full[2:])
    assert captured == [b"tiny"]


async def test_bad_tag_flushes_buffer(transport_with_capture):
    """A wrong HIF tag means stream desync — drop everything, same as kernel."""
    t, captured = transport_with_capture
    bogus = (4).to_bytes(2, "little") + (0xDEAD).to_bytes(2, "little") + b"junk"
    await t._handle_bulk_in(bogus)
    assert captured == []
    assert t._rx_buf == b""


async def test_recovers_after_bad_tag(transport_with_capture):
    """After flushing on bad tag, the next clean URB must parse normally."""
    t, captured = transport_with_capture
    bogus = (4).to_bytes(2, "little") + (0xDEAD).to_bytes(2, "little") + b"junk"
    good = _hif_chunk(_htc_frame(1, b"after-desync"))

    await t._handle_bulk_in(bogus)
    await t._handle_bulk_in(good)
    assert captured == [b"after-desync"]


async def test_padding_is_skipped_between_bundled_frames(transport_with_capture):
    """A 5-byte HTC payload + 8-byte HTC hdr = 13 B → 3 B pad. Next chunk must
    start AFTER that pad; otherwise the second frame's HIF header reads junk
    and the whole buffer is dropped."""
    t, captured = transport_with_capture
    urb = _hif_chunk(_htc_frame(1, b"five5")) + _hif_chunk(_htc_frame(1, b"abc"))
    await t._handle_bulk_in(urb)
    assert captured == [b"five5", b"abc"]


async def test_routing_dispatches_bulk_to_stream_parser(transport_with_capture):
    """_handle_incoming on USB_EP_DATA_WMI_IN goes through the stream parser."""
    t, captured = transport_with_capture
    await t._handle_incoming(_hif_chunk(_htc_frame(1, b"routed")), USB_EP_DATA_WMI_IN)
    assert captured == [b"routed"]


async def test_routing_dispatches_interrupt_directly(transport_with_capture):
    """Interrupt-IN (0x83) has no HIF wrapper — single HTC frame per URB."""
    t, captured = transport_with_capture
    await t._handle_incoming(_htc_frame(1, b"interrupt"), 0x83)
    assert captured == [b"interrupt"]


async def test_start_clears_rx_buf():
    """A leftover partial frame from a prior session must not leak into a new one."""
    dev = MagicMock(spec=usb.core.Device)
    t = AR9271USBTransport(dev)
    t._rx_buf.extend(b"stale-partial")
    await t.start()
    try:
        assert t._rx_buf == b""
    finally:
        await t.stop()
