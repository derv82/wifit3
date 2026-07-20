# Vendored binary provenance — `wdi-simple.exe`

`win-x64/wdi-simple.exe` is libwdi's `wdi-simple` example program, built **unmodified**
from a pinned upstream commit. It binds a USB device to WinUSB so libusb can open it
(the Tier-1 "Install WinUSB" action).

It is **unsigned**. libwdi's maintainer deliberately does not sign or distribute prebuilt
`wdi-simple.exe` binaries (<https://github.com/pbatard/libwdi/issues/309>), so every
consumer builds it, or trusts a recorded build. We vendor it and record full provenance
here so the binary is auditable: anyone can rebuild from the commit below and compare the
SHA-256. Invoking it requires UAC elevation (driver install is inherently privileged); it
is gated behind explicit user action in the app.

| field          | value |
|----------------|-------|
| upstream       | <https://github.com/pbatard/libwdi> |
| release        | v1.5.1 |
| commit         | `9b23b82a2dd1cbffc16d46c212f92c6bf8c0c602` |
| build workflow | `.github/workflows/vs2022.yml` (VS2022, `x64/Release`), unmodified |
| built via      | private mirror `derv82/libwdi-wdisimple-build`, run `27175685383` (disposable: provenance points at upstream, not the mirror) |
| packaging      | libwdi statically linked + WinUSB redist embedded → standalone exe, no loose DLLs |

## SHA-256

```
e7244932d58353f21b602be517b524293a868024421bb06c3f92ef3b61e73cab  win-x64/wdi-simple.exe
```

## Rebuild / verify

1. Fork or mirror `pbatard/libwdi` at commit `9b23b82`.
2. Run the `VS2022` GitHub Action (it downloads the WDK WinUSB redist and builds
   `x64/Release/examples/wdi-simple.exe`, then prints its SHA-256).
3. Download the `VS2022` artifact and compare the hash above.

arm64 Windows is not yet covered: the VS2022 workflow builds x64/Win32 only.
