"""Fetch a small icon per listed product domain into site/public/icons/<slug>.png (cached).

Uses DuckDuckGo's favicon service (or the GitHub owner's avatar for repo-only projects) at build time so the published site makes no third-party
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


def source(p):
    """Icon URL for a product: its site's favicon, or the GitHub owner's avatar for repo-only projects."""
    u = p.get("official_url") or ""
    if u:
        pu = urlparse(u if "://" in u else "https://" + u)
        h = (pu.hostname or "").removeprefix("www.")
        if h == "github.com":
            parts = [x for x in pu.path.split("/") if x]
            return f"https://github.com/{parts[0]}.png?size=64" if parts else None
        if h.endswith(".github.io"):
            return f"https://github.com/{h.split('.')[0]}.png?size=64"
        if h in SHARED_HOSTS:
            return None
        return f"https://icons.duckduckgo.com/ip3/{h}.ico"
    if p.get("github_repo"):
        return f"https://github.com/{p['github_repo'].split('/')[1]}.png?size=64"
    d = p.get("domain")
    return None if not d or d in SHARED_HOSTS else f"https://icons.duckduckgo.com/ip3/{d}.ico"


def fetch(client, slug, url):
    try:
        r = client.get(url, timeout=15, follow_redirects=True)
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
        src = source(json.loads((details / f"{s['slug']}.json").read_text()))
        if src:
            jobs.append((s["slug"], src))
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
