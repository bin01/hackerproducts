"""Fetch a small icon per listed product domain into site/public/icons/<slug>.png (cached).

Uses DuckDuckGo's favicon service at build time so the published site makes no third-party
requests. Products without a domain, or whose icon is missing or a placeholder, get a monogram.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "site" / "src" / "data" / "catalog.json"
OUT = ROOT / "site" / "public" / "icons"
MISS = ROOT / "data" / "icons_missing.json"


SHARED_HOSTS = {"github.com", "gitlab.com", "codeberg.org", "sr.ht", "git.sr.ht", "sourceforge.net", "google.com",
                "apple.com", "microsoft.com", "apps.apple.com", "play.google.com", "huggingface.co", "npmjs.com", "pypi.org",
                "crates.io", "addons.mozilla.org", "chromewebstore.google.com", "f-droid.org"}


def host(p):
    u = p.get("official_url") or ""
    if u:
        h = urlparse(u if "://" in u else "https://" + u).hostname or ""
        h = h.removeprefix("www.")
        return None if h in SHARED_HOSTS or h.endswith(".github.io") else h
    if p.get("github_repo"):
        return None
    d = p.get("domain")
    return None if d in SHARED_HOSTS else d


def fetch(client, slug, h):
    try:
        r = client.get(f"https://icons.duckduckgo.com/ip3/{h}.ico", timeout=15, follow_redirects=True)
    except httpx.HTTPError:
        return slug, False
    # DDG serves a generic placeholder for unknown sites; it is tiny or not an image.
    if r.status_code != 200 or len(r.content) < 200 or not r.headers.get("content-type", "").startswith("image"):
        return slug, False
    (OUT / f"{slug}.ico").write_bytes(r.content)
    return slug, True


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cat = json.loads(CATALOG.read_text())
    details = ROOT / "site" / "src" / "data" / "products"
    missing = set(json.loads(MISS.read_text())) if MISS.exists() else set()
    jobs = []
    for s in cat["products"]:
        if not s["listed"] or (OUT / f"{s['slug']}.ico").exists() or s["slug"] in missing:
            continue
        h = host(json.loads((details / f"{s['slug']}.json").read_text()))
        if h:
            jobs.append((s["slug"], h))
        else:
            missing.add(s["slug"])
    with httpx.Client(headers={"User-Agent": "hackerproducts-prototype"}) as client, ThreadPoolExecutor(12) as ex:
        for slug, ok in ex.map(lambda j: fetch(client, *j), jobs):
            if not ok:
                missing.add(slug)
    MISS.write_text(json.dumps(sorted(missing)))
    have = len(list(OUT.glob("*.ico")))
    print(f"icons: {have} fetched, {len(missing)} use a monogram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
