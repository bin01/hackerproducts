"""Fill in official homepages for ranked products that have none, with Opus. Cached by product name.

Output: data/resolve/official_v1.json {name: url or ""}. publish.py uses it when a product has no
official_url from synthesis; icons.py then fetches the icon for that host.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hp.sqlite"
CACHE = ROOT / "data" / "resolve" / "official_v1.json"


def main():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT p.name, c.name, p.domain, p.github_repo,
               (SELECT reason_quote FROM mention m WHERE m.product_id = p.id AND m.quote_verbatim = 1 ORDER BY confidence DESC LIMIT 1)
        FROM product p JOIN product_score s ON s.product_id = p.id JOIN category c ON c.id = p.category_id
        WHERE s.distinct_recommenders >= 3 AND coalesce(p.official_url, '') = ''
        ORDER BY s.distinct_recommenders DESC
    """).fetchall()
    todo = [r for r in rows if r[0] not in cache]
    print(f"official sites: {len(rows)} ranked products without one, {len(todo)} not yet looked up", flush=True)
    for start in range(0, len(todo), 120):
        chunk = todo[start:start + 120]
        lines = []
        for n, (name, cat, dom, repo, q) in enumerate(chunk):
            hint = f"; linked: {repo or dom}" if (repo or dom) else ""
            lines.append(f"{n}. {name} ({cat}{hint}; HN quote: \"{(q or '')[:100]}\")")
        prompt = ("For each numbered software product (named in Hacker News comments), give its official homepage URL. "
                  "Use the product's own website, not a GitHub repo, Wikipedia, an app store or a news article; if the "
                  "project only lives on GitHub/GitLab/Codeberg, give that repo URL. If you are not confident which product "
                  "is meant or what its site is, give \"\". Output one JSON object per line: {\"n\": <number>, \"url\": \"...\"}. "
                  "No other text.\n\n" + "\n".join(lines))
        try:
            text, _ = llm.run(prompt, model="opus", effort="medium", timeout=1800)
        except llm.UsageLimit as e:
            print(f"STOPPED: usage limit ({e}). Re-run to resume.")
            break
        got = {int(o["n"]): str(o.get("url") or "") for o in llm.parse_jsonl(text) if str(o.get("n", "")).isdigit()}
        for n, r in enumerate(chunk):
            if n in got:
                cache[r[0]] = got[n] if got[n].startswith("http") else ""
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))
        print(f"  {min(start + 120, len(todo))}/{len(todo)} looked up", flush=True)
    print(f"official sites known: {sum(bool(v) for v in cache.values())} of {len(cache)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
