"""List icons that would vanish on the dark theme (dark marks on a transparent background) for Icon.astro to back with a light tile."""
import json
import warnings
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "site" / "public" / "icons"
OUT = ROOT / "site" / "src" / "data" / "dark_icons.json"


def needs_tile(path):
    img = Image.open(path)
    if img.format == "ICO" and img.info.get("sizes"):
        img.size = max(img.info["sizes"])
    img = img.convert("RGBA").resize((32, 32))
    px = list(img.getdata())
    solid = [p for p in px if p[3] > 128]
    if not solid or len(solid) > 0.9 * len(px):  # blank, or has its own background
        return False
    dark = sum(max(p[:3]) < 100 for p in solid)  # by brightest channel: a saturated blue reads fine on dark
    return dark / len(solid) >= 0.7


def main():
    warnings.simplefilter("ignore")
    found, bad = [], 0
    for f in sorted(ICONS.glob("*.ico")):
        try:
            if needs_tile(f):
                found.append(f.stem)
        except Exception:
            bad += 1
    OUT.write_text(json.dumps(found))
    print(f"{len(found)} of {len(list(ICONS.glob('*.ico')))} icons need a light tile in dark mode; {bad} unreadable")


if __name__ == "__main__":
    main()
