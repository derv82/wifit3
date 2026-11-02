"""With the vault open on Uncracked-500, save a handshake through Vault.save_handshake as the scanner does,
for Heavy-200 and then for an AP new to the vault; print the time until the AP list has painted each."""
import asyncio
import random
import time

from make_vault_fixture import RSN_IE, data_frame, m1_m2, mac_bytes
from vault_bench import bssid_of, open_vault, painted, select, terminal_size, wait_for
from wifit3.models import AccessPoint, Handshake, HandshakeMessage
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault_table import VaultTable

AKM_PSK = 2
CLIENT = "02:00:00:0b:00:00"


def access_point_with_handshake(ssid: str, bssid: str, psk: str) -> AccessPoint:
    """An AP heard beaconing, with an M1+M2 from ``CLIENT`` that ``psk`` cracks."""
    messages = m1_m2(psk, ssid, bssid, CLIENT, random.Random())
    replay = (1).to_bytes(8, "big").hex()
    aa, spa = mac_bytes(bssid), mac_bytes(CLIENT)
    handshake = Handshake(bssid=bssid, client_mac=CLIENT, beacon_frame=b"BEACON", akm_offered=[AKM_PSK],
                          rsn_ie_seen=True)
    now = time.time()
    handshake.add_message(HandshakeMessage(
        raw=data_frame(aa, spa, messages.m1), msg_num=1, replay_hex=replay, nonce=messages.anonce,
        mic=bytes(16), key_data_len=0, eapol_payload=messages.m1, akm=AKM_PSK, timestamp=now))
    handshake.add_message(HandshakeMessage(
        raw=data_frame(aa, spa, messages.m2), msg_num=2, replay_hex=replay, nonce=messages.snonce,
        mic=messages.mic, key_data_len=len(RSN_IE), eapol_payload=messages.m2, akm=AKM_PSK,
        timestamp=now + 0.01))
    ap = AccessPoint(bssid=bssid, ssid=ssid)
    ap.handshakes[CLIENT] = handshake
    return ap


async def main() -> None:
    app = WifiteApp(cli_log_level="unset")
    async with app.run_test(size=terminal_size()) as pilot:
        await pilot.pause()
        drawer, _ = await open_vault(app)
        await select(drawer, bssid_of(app, "Uncracked-500"))
        table = drawer.query_one("#vault-table", VaultTable)
        for ssid, bssid, psk in (("Heavy-200", bssid_of(app, "Heavy-200"), "new-password-3"),
                                 ("Captured-New", "02:00:00:0c:00:00", "new-ap-password")):
            await asyncio.sleep(1.0)
            ap = access_point_with_handshake(ssid, bssid, psk)
            started = time.perf_counter()
            if app.vault.save_handshake(ap, CLIENT) is None:
                raise RuntimeError(f"{ssid}: the handshake was not saved")
            saved = app.vault.persisted(bssid)[0]
            await wait_for(lambda: any(capture is saved for capture in table._aps.get(bssid, (None, []))[1]))
            print(f"{ssid}: {(await painted(drawer) - started) * 1000:.0f} ms from save_handshake "
                  f"to the AP list painted")


if __name__ == "__main__":
    asyncio.run(main())
