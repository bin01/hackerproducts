"""Hand-checked logo sources for ranked products the automatic favicon step missed. Writes 64px PNG bytes to icons/<slug>.ico.

Order of preference: the product's own site (largest declared icon), a GitHub organization's avatar when its name matches,
Wikimedia Commons for logos sites don't expose well, or the logo of the parent model family. Personal avatars are never used.
"""
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import httpx
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "public" / "icons"
MISS = ROOT / "data" / "icons_missing.json"
SHEET = ROOT / "data" / "logo_contact_sheet.png"
COMMONS = "https://commons.wikimedia.org/wiki/Special:FilePath/{}?width=128"

SITE = {"pi": "pi.dev", "glm": "z.ai", "kimi": "www.kimi.com", "vim": "www.vim.org", "htmx": "htmx.org", "codeberg": "codeberg.org",
        "handy": "handy.computer", "cachyos": "cachyos.org", "f-droid": "f-droid.org", "gitea": "about.gitea.com", "astro": "astro.build",
        "anki": "apps.ankiweb.net", "openstreetmap": "www.openstreetmap.org", "radicle": "radicle.dev", "polars": "pola.rs",
        "vlc-media-player": "www.videolan.org", "vite": "vite.dev", "kitty": "sw.kovidgoyal.net/kitty/",
        "fil-c": "fil-c.org", "reticulum": "reticulum.network", "streetcomplete": "streetcomplete.app", "headscale": "headscale.net",
        "meshtastic": "meshtastic.org", "servo": "servo.org", "vscodium": "vscodium.com", "mercurial": "www.mercurial-scm.org",
        "vue-js": "vuejs.org", "logseq": "logseq.com", "prometheus": "prometheus.io", "pandoc": "pandoc.org",
        "macos": "www.apple.com", "ios": "www.apple.com", "apple-passwords": "www.apple.com", "apple-carplay": "www.apple.com", "safari": "www.apple.com",
        "microsoft-windows": "www.microsoft.com", "windows-11": "www.microsoft.com", "google-search": "www.google.com"}
ORG = {"github": "github", "github-copilot": "github", "codex-cli": "openai", "whisper": "openai", "nvidia-parakeet": "nvidia",
       "tlaplus": "tlaplus", "vaultwarden": "vaultwarden"}
ORG_FALLBACK = {"htmx": "bigskysoftware", "astro": "withastro", "vite": "vitejs", "vue-js": "vuejs", "polars": "pola-rs", "cachyos": "CachyOS",
                "anki": "ankitects", "radicle": "radicle-dev", "streetcomplete": "streetcomplete", "meshtastic": "meshtastic", "servo": "servo",
                "prometheus": "prometheus", "glm": "zai-org", "mercurial": "mercurial-scm", "headscale": "headscale"}
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WIKI = {"google-chrome": "Google_Chrome_icon_(February_2022).svg",
        "google-maps": "Google_Maps_icon_(2020).svg", "microsoft-excel": "Microsoft_Office_Excel_(2019–2025).svg", "nextstep": "NeXT_logo.svg"}
PARENT = {"nano-banana": "gemini"}  # Google's image model
# Tier pages shown on family pages reuse the family's logo; found by family in the catalog.


def largest(img):
    """Best frame of an .ico or the image itself, as RGBA."""
    if getattr(img, "format", "") == "ICO":
        sizes = sorted(img.info.get("sizes", []), key=lambda s: s[0])
        if sizes:
            img.size = sizes[-1]
    return img.convert("RGBA")


def is_org(name, cache={}):
    if name not in cache:
        r = subprocess.run(["gh", "api", f"users/{name}", "-q", ".type"], capture_output=True, text=True)
        cache[name] = r.stdout.strip() == "Organization"
    return cache[name]


def rasterize_svg(data):
    """Render an SVG as an <img> (so its scripts can't run) in headless Chrome, on a transparent background."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "i.svg").write_bytes(data)
        (Path(d) / "i.html").write_text('<!doctype html><body style="margin:0;background:transparent"><img src="i.svg" width="128" height="128"></body>')
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox=false", "--hide-scrollbars", "--window-size=128,128", "--default-background-color=00000000",
                        f"--screenshot={d}/o.png", f"file://{d}/i.html"], capture_output=True, timeout=60)
        return (Path(d) / "o.png").read_bytes()


def on_tile_if_pale(img):
    """A mostly white logo on a transparent background vanishes in light mode: put it on a dark rounded tile."""
    small = img.resize((32, 32))
    px = [p for p in small.getdata() if p[3] > 128]
    if not px or sum(0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2] > 215 for p in px) / len(px) < 0.5 or len(px) > 0.9 * 32 * 32:
        return img
    side = img.width
    tile = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, side - 1, side - 1), side // 5, fill="#1d1d1b")
    tile.alpha_composite(img)
    return tile


def normalize(data):
    if data[:200].lstrip().startswith((b"<svg", b"<?xml")) or b"<svg" in data[:1000]:
        data = rasterize_svg(data)
    img = largest(Image.open(io.BytesIO(data)))
    if min(img.size) < 16:
        raise ValueError(f"too small {img.size}")
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    canvas = on_tile_if_pale(canvas)
    if canvas.getextrema()[3][1] == 0:
        raise ValueError("blank")
    if side > 64:
        canvas = canvas.resize((64, 64), Image.LANCZOS)
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    return buf.getvalue(), side


def from_site(client, host):
    base = f"https://{host}"
    urls = []
    try:
        r = client.get(base)
        html, base = r.text, str(r.url)
        for tag in re.findall(r"<link\b[^>]*>", html, re.I):
            rel = re.search(r'rel=["\']?([^"\'>]+)', tag, re.I)
            href = re.search(r'href=["\']?([^"\'\s>]+)', tag, re.I)
            if rel and href and "icon" in rel[1].lower() and "mask" not in rel[1].lower():
                size = re.search(r'sizes=["\']?(\d+)', tag, re.I)
                svg = ".svg" in href[1]
                urls.append((128 if svg else int(size[1]) if size else (180 if "apple" in rel[1].lower() else 32), urljoin(base, href[1])))
    except httpx.HTTPError:
        pass
    ordered = [u for _, u in sorted(urls, key=lambda x: -x[0])] + [urljoin(base, "/apple-touch-icon.png"), urljoin(base, "/favicon.svg"), urljoin(base, "/favicon.ico")]
    return ordered


def main():
    cat = json.loads((ROOT / "site" / "src" / "data" / "catalog.json").read_text())
    added, failed = {}, {}
    with httpx.Client(headers={"User-Agent": "HackerProductsLogoFetch/1.0 (https://hackerproducts.pages.dev)"}, follow_redirects=True, timeout=20) as c:
        jobs = {}
        for s, h in SITE.items():
            fb = ORG_FALLBACK.get(s)
            jobs[s] = lambda h=h, fb=fb: from_site(c, h) + ([f"https://github.com/{fb}.png?size=128"] if fb and is_org(fb) else [])
        for s, o in ORG.items():
            jobs[s] = lambda o=o: [f"https://github.com/{o}.png?size=128"]
        jobs["codeberg"] = lambda: ["https://codeberg.org/assets/img/logo.png", "https://codeberg.org/assets/img/favicon.png"]
        for s, w in WIKI.items():
            jobs[s] = lambda w=w: [COMMONS.format(w)]
        for slug, get in jobs.items():
            if slug in ORG and not is_org(ORG[slug]):
                failed[slug] = f"{ORG[slug]} is not an organization"
                continue
            for u in get():
                try:
                    r = c.get(u)
                    if r.status_code != 200:
                        continue
                    png, side = normalize(r.content)
                except Exception:
                    continue
                (OUT / f"{slug}.ico").write_bytes(png)
                added[slug] = (u, side)
                break
            else:
                failed[slug] = "no usable image"
    # Parents and tiers: reuse the family's logo.
    fam = {p["slug"]: p["family"] for p in cat["products"] if p.get("family") and p["listed"]}
    for child, parent in {**fam, **PARENT}.items():
        src, dst = OUT / f"{parent}.ico", OUT / f"{child}.ico"
        if src.exists() and not dst.exists():
            dst.write_bytes(src.read_bytes())
            added[child] = (f"family:{parent}", 0)
    miss = set(json.loads(MISS.read_text())) - set(added) if MISS.exists() else set()
    MISS.write_text(json.dumps(sorted(miss)))
    # Contact sheet for review.
    slugs = sorted(added)
    cols = 8
    rows = len(slugs) // cols + 1
    sheet = Image.new("RGB", (cols * 170, rows * 110), "#f6f6ef")
    dark = Image.new("RGB", (64, 64), "#161614")
    d = ImageDraw.Draw(sheet)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Verdana.ttf", 11)
    for i, s in enumerate(slugs):
        x, y = (i % cols) * 170 + 8, (i // cols) * 110 + 6
        im = Image.open(OUT / f"{s}.ico").convert("RGBA").resize((64, 64))
        sheet.paste(im, (x, y), im)
        sheet.paste(dark, (x + 70, y))
        sheet.paste(im, (x + 70, y), im)
        d.text((x, y + 70), f"{s[:22]}", font=font, fill="#1d1d1b")
        d.text((x, y + 84), f"{added[s][1] or 'parent'}", font=font, fill="#6e6d66")
    sheet.save(SHEET)
    print(f"added {len(added)}, failed {len(failed)}")
    print("failed:", failed)
    print("sources:", {s: added[s][0][:60] for s in slugs if not added[s][0].startswith("family")})


if __name__ == "__main__":
    main()
