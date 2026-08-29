# Single source of truth for the version. pyproject.toml derives [project].version from
# this literal at build time (hatchling dynamic version, [tool.hatch.version]), and the
# release workflow gates the pushed tag against it. Kept as a plain literal so the frozen
# PyInstaller binary reports it from `wifit3 --version` without bundling dist metadata.
__version__ = "0.1.3"
