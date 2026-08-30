"""GitHub release update checks."""
from __future__ import annotations

from dataclasses import dataclass
import json
import platform as platform_module
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_RELEASES_URL = "https://api.github.com/repos/derv82/wifit3/releases"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[.-].*)?$")
AUTO_CHECK_UPDATES_DEFAULT = True
AUTO_UPDATE_DEFAULT = False


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    release_url: str
    prerelease: bool
    assets: tuple[ReleaseAsset, ...]


@dataclass(frozen=True)
class UpdateInfo:
    """Result of comparing the local version with the latest published release."""
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str
    current_version_known: bool = True


@dataclass(frozen=True)
class UpdatePlan:
    """Future updater input: which release asset would be used for this platform."""
    update: UpdateInfo
    asset_name: str | None = None
    asset_url: str | None = None
    auto_update_enabled: bool = AUTO_UPDATE_DEFAULT


class UpdateCheckError(Exception):
    """Raised when GitHub release data cannot be fetched or parsed."""


def print_update_check(current_version: str) -> int:
    """Print a human-readable update check result; return a process exit code."""
    try:
        update = check_for_updates(current_version)
    except UpdateCheckError as e:
        print(e)
        return 2
    print(f"current: {update.current_version}")
    print(f"latest: {update.latest_version}  {update.release_url}")
    if not update.current_version_known:
        print("warning: current version is not one of the published GitHub Releases")
    if update.update_available:
        print(f"wifit3 {update.latest_version} is available: {update.release_url}")
    return 0


def check_for_updates(current_version: str, timeout: float = 2.0) -> UpdateInfo:
    """Fetch GitHub releases and compare the latest stable release to ``current_version``."""
    releases = _fetch_releases(timeout)
    latest = _latest_stable_release(releases)
    current = current_version.removeprefix("v")
    current_key = _version_key(current)
    known_versions = {release.version for release in releases}
    return UpdateInfo(
        current_version=current,
        latest_version=latest.version,
        update_available=_version_key(latest.version) > current_key,
        release_url=latest.release_url,
        current_version_known=current in known_versions,
    )


def plan_update(current_version: str, timeout: float = 2.0, *, package_format: str = "binary",
                system: str | None = None, machine: str | None = None) -> UpdatePlan:
    """Return the release asset a future updater should use; never downloads or installs."""
    releases = _fetch_releases(timeout)
    latest = _latest_stable_release(releases)
    update = UpdateInfo(
        current_version=current_version.removeprefix("v"),
        latest_version=latest.version,
        update_available=_version_key(latest.version) > _version_key(current_version),
        release_url=latest.release_url,
        current_version_known=current_version.removeprefix("v") in {r.version for r in releases},
    )
    if not update.update_available:
        return UpdatePlan(update)
    asset = _select_asset(latest, package_format=package_format, system=system, machine=machine)
    return UpdatePlan(update, asset.name if asset else None, asset.download_url if asset else None)


def _fetch_releases(timeout: float) -> tuple[ReleaseInfo, ...]:
    data = _fetch_json(_RELEASES_URL, timeout)
    if not isinstance(data, list):
        raise UpdateCheckError("GitHub releases response was not a list")
    releases = tuple(_parse_release(item) for item in data if isinstance(item, dict))
    if not releases:
        raise UpdateCheckError("GitHub releases response did not include any valid releases")
    return releases


def _fetch_latest_release(timeout: float) -> tuple[str, str]:
    latest = _latest_stable_release(_fetch_releases(timeout))
    return latest.version, latest.release_url


def _fetch_json(url: str, timeout: float) -> object:
    req = Request(url, headers={"Accept": "application/vnd.github+json",
                               "User-Agent": "wifit3-update-check"})
    try:
        with urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        raise UpdateCheckError(f"failed to check for updates: {e}") from e


def _parse_release(data: dict) -> ReleaseInfo:
    tag = data.get("tag_name")
    url = data.get("html_url") or "https://github.com/derv82/wifit3/releases/latest"
    if not isinstance(tag, str) or _version_key(tag) == (0, 0, 0):
        raise UpdateCheckError("release response did not include a valid version tag")
    if not isinstance(url, str):
        raise UpdateCheckError("release response did not include a valid release URL")
    assets = tuple(_parse_asset(asset) for asset in data.get("assets", ()) if isinstance(asset, dict))
    return ReleaseInfo(tag.removeprefix("v"), url, bool(data.get("prerelease")), assets)


def _parse_asset(data: dict) -> ReleaseAsset:
    name = data.get("name")
    url = data.get("browser_download_url")
    if not isinstance(name, str) or not isinstance(url, str):
        raise UpdateCheckError("release asset response did not include a valid name and download URL")
    return ReleaseAsset(name, url)


def _latest_stable_release(releases: tuple[ReleaseInfo, ...]) -> ReleaseInfo:
    stable = [release for release in releases if not release.prerelease and _version_key(release.version) != (0, 0, 0)]
    if not stable:
        raise UpdateCheckError("GitHub releases response did not include a stable release")
    return max(stable, key=lambda release: _version_key(release.version))


def _select_asset(release: ReleaseInfo, *, package_format: str, system: str | None,
                  machine: str | None) -> ReleaseAsset | None:
    pattern = _asset_pattern(package_format, system or platform_module.system(), machine or platform_module.machine())
    for asset in release.assets:
        if pattern.fullmatch(asset.name):
            return asset
    return None


def _asset_pattern(package_format: str, system: str, machine: str) -> re.Pattern[str]:
    normalized_system = system.lower()
    normalized_machine = machine.lower()
    is_x64 = normalized_machine in {"x86_64", "amd64"}
    if normalized_system == "linux" and is_x64 and package_format == "deb":
        return re.compile(r"wifit3_\d+\.\d+\.\d+.*_amd64\.deb")
    if normalized_system == "linux" and is_x64 and package_format == "arch":
        return re.compile(r"wifit3-bin-\d+\.\d+\.\d+.*-1-x86_64\.pkg\.tar\.zst")
    if normalized_system == "linux" and is_x64:
        return re.compile(r"wifit3-linux-x64")
    if normalized_system == "darwin":
        return re.compile(r"wifit3-macos-universal2")
    if normalized_system == "windows" and is_x64:
        return re.compile(r"wifit3-windows-x64\.exe")
    return re.compile(r"a^")


def _version_key(version: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(version.strip())
    if match is None:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())
