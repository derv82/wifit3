from typing import Any, Optional

from textual.app import ComposeResult
from textual.widgets import Checkbox, Label

from wifit3.crack.passwords import DEFAULT_PASSWORDS_PATH, read_passwords
from wifit3.persist.config import Config, CpuCrackSettings
from wifit3.ui.vault.modals.wordlists import WordlistConfigModal

_ABOUT = ("[b]Note:[/b] CPU Crack is wifit3's built-in WPA cracker.\n"
          "      May cause battery drain. Use at your own risk.")


class CpuCrackConfigModal(WordlistConfigModal):
    """Configuration modal for CPU Crack jobs."""

    dialog_title = "CPU Crack"

    DEFAULT_CSS = """
    CpuCrackConfigModal #cpu-crack-about { margin: 1 0; }
    CpuCrackConfigModal #cpu-crack-default-passwords { margin-top: 1; }
    """

    def _settings(self) -> CpuCrackSettings:
        return Config.cpu_crack

    def _compose_fields(self) -> ComposeResult:
        settings = Config.cpu_crack
        default_count = sum(1 for _ in read_passwords(DEFAULT_PASSWORDS_PATH))
        yield Label(_ABOUT, id="cpu-crack-about")
        yield from self._compose_wordlists()
        yield Checkbox(f"Default passwords ({default_count:,})", value=settings.default_passwords,
                       tooltip=f"RockYou's top {default_count:,} passwords by frequency",
                       id="cpu-crack-default-passwords")
        yield Checkbox("This AP's SSID variants", value=settings.ap_ssid_passwords,
                       tooltip="Passwords based on the AP's SSID: lowercase, uppercase, and "
                               "common password padding",
                       id="cpu-crack-ap-ssid-passwords")
        yield Checkbox("Probed SSIDs (this session)", value=settings.probed_ssid_passwords,
                       tooltip="Passwords based on SSIDs probed this session (clients may "
                               "mistype the PSK into the SSID field)",
                       id="cpu-crack-probed-ssid-passwords")

    def _launch_config(self) -> Optional[dict[str, Any]]:
        config = {"wordlists": list(self._wordlists),
                  "default_passwords": self._checked("#cpu-crack-default-passwords"),
                  "ap_ssid_passwords": self._checked("#cpu-crack-ap-ssid-passwords"),
                  "probed_ssid_passwords": self._checked("#cpu-crack-probed-ssid-passwords")}
        if not any(config.values()):
            self.notify("Please choose a wordlist or a password source", severity="error")
            return None
        return config

    def _checked(self, selector: str) -> bool:
        return self.query_one(selector, Checkbox).value

    def _store_settings(self, config: dict[str, Any]) -> None:
        super()._store_settings(config)
        settings = Config.cpu_crack
        settings.default_passwords = config["default_passwords"]
        settings.ap_ssid_passwords = config["ap_ssid_passwords"]
        settings.probed_ssid_passwords = config["probed_ssid_passwords"]
