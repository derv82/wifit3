"""A campaign's scoped hold on one interface: arm config on enter, restore it on exit."""
from __future__ import annotations

from typing import Any, Optional

# Sentinel for ``fake_mac``: let the driver pick a random locally-administered MAC
# (SPOOFABLE cards) or fall back to the card's own MAC (FIXED_MAC cards).
SPOOFABLE = object()


class Lease:
    """One interface, configured on enter and restored on exit (channel, fake MAC, ACK tally).
    The armed MAC is registered as own so ingest drops our echo; exit clears everything."""

    def __init__(self, array, iface, *, channel: Optional[int] = None,
                 fake_mac: Any = None, bssid: Any = None, ack_tally: bool = False) -> None:
        self._array = array
        self.iface = iface
        self._channel = channel
        self._fake_mac = fake_mac
        self._bssid = bssid
        self._ack_tally = ack_tally
        self._orig_channel: Optional[int] = None
        self._own_mac: Optional[str] = None

    async def __aenter__(self):
        self._orig_channel = self.iface.current_channel
        if self._channel is not None:
            await self.iface.set_channel(self._channel)
        if self._fake_mac is not None:
            requested = None if self._fake_mac is SPOOFABLE else self._fake_mac
            armed = await self.iface.set_fake_mac(requested, self._bssid)
            if armed:
                self._own_mac = self._array.register_own_mac(armed)
        if self._ack_tally:
            await self.iface.enable_rx_acks()
        return self.iface

    async def __aexit__(self, *exc) -> bool:
        if self._ack_tally:
            await self.iface.disable_rx_acks()
        if self._own_mac is not None:
            await self.iface.clear_fake_mac()
            self._array.unregister_own_mac(self._own_mac)
        if self._channel is not None and self._orig_channel is not None:
            await self.iface.set_channel(self._orig_channel)
        return False

    @property
    def mac(self) -> Optional[str]:
        """The forged MAC the interface is ACKing as, or None when no fake MAC was armed."""
        return self._own_mac
