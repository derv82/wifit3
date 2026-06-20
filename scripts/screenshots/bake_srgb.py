"""Bake a DCI-P3-OLED screenshot into plain sRGB so it looks the same elsewhere.

The Snipping Tool captures raw framebuffer numbers tagged sRGB; on a DCI-P3 OLED
those numbers display darker/richer than sRGB intends, and that look lives in the
panel, not the file. To make a screenshot read on a normal monitor the way it
looks on the P3 panel, interpret the numbers through the panel's measured DCI-P3
profile and convert to sRGB — baking the panel's transform into the pixels.
Reproducible; no GIMP. (The OLED's true-black contrast is the panel, not the
file, so it can't transfer — only the gamut + gamma shift do.)

    uv run --with Pillow python scripts/screenshots/bake_srgb.py <png>...

Writes <name>.png under screenshots/baked/, tagged sRGB.
"""
import os
import sys

from PIL import Image, ImageCms

# The factory profile for the ThinkPad OLED panel in its DCI-P3 mode.
PROFILE = r"C:\Windows\System32\spool\drivers\color\TPLCD_420B_DCIP3.icm"
OUTDIR = "screenshots/baked"


def _intent():
    try:
        return ImageCms.Intent.RELATIVE_COLORIMETRIC
    except AttributeError:
        return ImageCms.INTENT_RELATIVE_COLORIMETRIC


def _bpc_flag():
    try:
        return int(ImageCms.Flags.BLACKPOINTCOMPENSATION)
    except AttributeError:
        return 8192


def bake(path: str) -> str:
    src = ImageCms.getOpenProfile(PROFILE)
    dst = ImageCms.createProfile("sRGB")
    im = Image.open(path).convert("RGB")
    out = ImageCms.profileToProfile(
        im, src, dst, renderingIntent=_intent(), outputMode="RGB", flags=_bpc_flag()
    )
    os.makedirs(OUTDIR, exist_ok=True)
    outpath = os.path.join(OUTDIR, os.path.basename(path))
    out.save(outpath, icc_profile=ImageCms.ImageCmsProfile(dst).tobytes())
    return outpath


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print("baked:", bake(p))
