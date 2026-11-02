## Wireless Card Art
These `.ans` are the 20x10 ANSI pixel art images shown in wifit3's Focus view.

### Background & LED Colors
Background: wifit3 replaces pixels with black background (empty foreground) with "transparent" pixels - see `make_black_transparent` in `wifit3.ui.ansi_art`.

LED: wifit3 replaces pixels with dark green foreground `(0, 128, 0)` with "breathing" green pixels - see `breathe()` in `wifit3.ui.screens.focus_v2.art`. The LED blinks when TX is transmitted.

### PNGs for SUPPORTED-HARDWARE.md
`scripts/ui/render_cardart.py` rasterizes each `.ans` into a transparent-background `.png` under `assets/cardart/`, which is what the driver sections of `docs/SUPPORTED-HARDWARE.md` point at.

- It is never run automatically. After adding or changing art: `uv run python ./scripts/ui/render_cardart.py`
- `_LABELS` maps a card's stem to the make+model caption drawn underneath the art.
- `_STACKS` names a group of cards to render as one vertically-stacked image, caption under each, for drivers that support more than one card. These lists are hand-managed, so a new card only joins a stack when you add it.

### Mapping VID:PID to Card Art
1. The driver's `__init__` contains VID:PIDs under "SUPPORTED_IDS"; the desired vid:pid *should* make use of a constant model name in `wifit3.chips.products` (add if needed).
2. The `_ART_BY_PRODUCT` dict will map the products.py Vendor.MODEL_NAME constant to the `.ans` file path, in `wifit3.ui.screens.focus_v2.art`

### Disambiguating overlapping VID:PIDs
Some devices use the same VID:PID as other devices. We hate these devices.

One way to differentiate the make/model of devices with matching VID:PIDs is to look at the MAC address' Vendor OUI field.

You can override the appropriate Driver's `product_name` at any point of bringup, once key information is retrievable.
- We do this for `0E8D:7961` (Alfa AWUS036AXML & Panda PAU0F devices), which share the same vid:pid - see `derive_product_name()` in `wifit3.chips.mt7921au.driver.MT7921AUDriver`
- And for `0CF3:9271` (Alfa AWUS036NHA & TP-Link TL-WN722N v1) - see `derive_product_name()` in `wifit3.chips.ar9271_v2.driver.AR9271V2Driver`
- And for the Panda PAU0B, whose OUI shows up in the EFUSE MAC during bringup - see `wifit3.chips.mt76x0u.driver.MT76x0UDriver.connect`

Until the OUI is read, these devices carry a combined `"Alfa X / Panda Y"` label from `wifit3.chips.products`, so `_ART_BY_PRODUCT` also maps that combined label to whichever card art should show in the meantime.

The other way is when a VID:PID is claimed by two *drivers*, not two cards. `_VIDPID_FAMILIES` in `wifit3.device.manager` picks the driver package before anything is imported, and the winning package's `SUPPORTED_IDS` row brings its own `product_name` (and therefore its own art) with it.
- `2357:0137` is a MediaTek MT7612U or a Realtek RTL8822CU, told apart by whether the device has bulk-IN endpoint `0x85` - see `_resolve_2357_0137`
- `148F:760A` is an MT7601U or an MT7610U, told apart by one vendor read of the silicon ID - see `_resolve_148f_760a`

### When the model can't be pinned down at all
Art can also be mapped by chipset alone, via the `_ART_BY_CHIPSET` dict in `wifit3.ui.screens.focus_v2.art`. This is the fallback for a chipset that only ships in one physical card we care about, and it's the last thing tried before the generic art.

