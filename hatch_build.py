"""Build-time metadata hook: Termux uses the system libusb, not the bundled glibc wheel."""
import os

from hatchling.metadata.plugin.interface import MetadataHookInterface


def _on_termux() -> bool:
    return bool(os.environ.get("TERMUX_VERSION")) or os.environ.get(
        "PREFIX", "").endswith("/com.termux/files/usr")


class CustomMetadataHook(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        if _on_termux():
            metadata["dependencies"] = [
                dependency for dependency in metadata.get("dependencies", [])
                if not dependency.startswith("libusb-package")
            ]
