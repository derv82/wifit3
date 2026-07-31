# Vendored binary provenance: `wdi-simple.exe`

`win-x64/wdi-simple.exe` and `win-arm64/wdi-simple.exe` are libwdi's `wdi-simple` example
program, built from a pinned upstream commit. wifit3 picks the one matching the running CPU
at install time (`setup/windows.py` `_ARCH_DIRS`). It binds a USB device to WinUSB so libusb
can open it (the Tier-1 "Install WinUSB" action).

It is **unsigned**. libwdi's maintainer deliberately does not sign or distribute prebuilt
`wdi-simple.exe` binaries (<https://github.com/pbatard/libwdi/issues/309>), so every
consumer builds it, or trusts a recorded build. We vendor it and record full provenance
here so the binary is auditable: anyone can rebuild from the commit below and compare the
SHA-256. Invoking it requires UAC elevation (driver install is inherently privileged); it is
only reached through explicit user action in the app.

The libwdi C source is upstream and unmodified for both arches. The x64/Win32 build runs the
upstream `VS2022` workflow unchanged. The arm64 build adds a `Debug|ARM64` / `Release|ARM64`
solution config to the VS projects (no source changes) plus CI changes to build it (install
the VC ARM64 toolset, pin the runner to `windows-2022`). That diff lives on the mirror's
`arm64` branch and is auditable.

| field    | value |
|----------|-------|
| upstream | <https://github.com/pbatard/libwdi> |
| release  | v1.5.1 |
| source   | commit `9b23b82a2dd1cbffc16d46c212f92c6bf8c0c602` (libwdi C, unmodified) |
| packaging| libwdi statically linked + WinUSB redist embedded, standalone exe, no loose DLLs |

### win-x64
| field | value |
|-------|-------|
| build | `.github/workflows/vs2022.yml` (VS2022, `x64/Release`), unmodified |
| via   | mirror `derv82/libwdi-wdisimple-build` `master`, run `27175685383` |

### win-arm64
| field | value |
|-------|-------|
| build | mirror branch `arm64` @ `646d5b2` (adds ARM64 solution/project configs + CI), `ARM64/Release` |
| via   | run `30625385217` |

## SHA-256

```
e7244932d58353f21b602be517b524293a868024421bb06c3f92ef3b61e73cab  win-x64/wdi-simple.exe
3feb0824104b5a523b9d5afbee9db1a74c4dddb826f97a47dbd7cb924cb23f14  win-arm64/wdi-simple.exe
```

## Rebuild / verify

1. Fork or mirror `pbatard/libwdi` at commit `9b23b82`.
2. x64/Win32: run the `VS2022` GitHub Action unmodified. arm64: apply the `arm64` branch
   changes (ARM64 solution config + the ARM64 toolset-install and `windows-2022` CI steps).
3. The Action downloads the WDK WinUSB redist, builds `<arch>/Release/examples/wdi-simple.exe`,
   and prints its SHA-256. Download the `VS2022` artifact and compare the hashes above.
