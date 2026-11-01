"""Stage 7: export data/hp.sqlite to JSON for the Astro site and the public JSON export.

site/src/data/catalog.json           lists, categories, stats
site/src/data/products/<slug>.json   one per product page
site/public/export/*.json            downloadable data (full catalog, per category)
"""
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hp.sqlite"
SITE = ROOT / "site"
DATA = SITE / "src" / "data"
EXPORT = SITE / "public" / "export"
MONTHS = [f"{y}-{m:02d}" for y, m in [(2025, 11), (2025, 12)] + [(2026, i) for i in range(1, 11)]]
MIN_PAGE = 2   # distinct non-affiliated commenters for a product page
MIN_LIST = 3   # distinct recommenders to appear in ranked lists


def hn(item_id):
    return f"https://news.ycombinator.com/item?id={item_id}"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cats = {r["id"]: dict(r) for r in con.execute("SELECT * FROM category")}
    scores = {r["product_id"]: dict(r) for r in con.execute("SELECT * FROM product_score")}
    verdicts = {r["product_id"]: dict(r) for r in con.execute("SELECT * FROM verdict")}
    themes = defaultdict(list)
    for r in con.execute("SELECT * FROM theme ORDER BY id"):
        themes[r["product_id"]].append(dict(r))
    mentions = defaultdict(list)
    for r in con.execute("""SELECT m.*, i.by, i.time, i.story_id, i.story_title, i.text AS comment
                            FROM mention m JOIN hn_item i ON i.id = m.item_id ORDER BY i.time DESC"""):
        mentions[r["product_id"]].append(dict(r))
    products = {r["id"]: dict(r) for r in con.execute("SELECT * FROM product")}
    comps = defaultdict(list)
    for r in con.execute("SELECT * FROM comparison WHERE co_mentions + switched_a_to_b + switched_b_to_a >= 2"):
        comps[r["product_a"]].append((r["product_b"], r["co_mentions"], r["switched_b_to_a"], r["switched_a_to_b"]))
        comps[r["product_b"]].append((r["product_a"], r["co_mentions"], r["switched_a_to_b"], r["switched_b_to_a"]))

    paged = {pid for pid, s in scores.items() if s["distinct_commenters"] >= MIN_PAGE and s["distinct_recommenders"] >= 1}

    def quote_obj(m):
        return {"id": m["id"], "item_id": m["item_id"], "url": hn(m["item_id"]), "by": m["by"], "date": m["time"][:10],
                "stance": m["stance"], "quote": m["reason_quote"] if m["quote_verbatim"] else "",
                "excerpt": " ".join((m["comment"] or "").split())[:420], "context": m["recommender_context"],
                "use_case": m["use_case"], "switched_from": m["switched_from"], "story": m["story_title"]}

    summaries = []
    shutil.rmtree(EXPORT, ignore_errors=True)
    EXPORT.mkdir(parents=True)
    shutil.rmtree(DATA / "products", ignore_errors=True)
    (DATA / "products").mkdir(parents=True)
    for pid in paged:
        p, s = products[pid], scores[pid]
        ms = [m for m in mentions[pid] if not m["affiliated"]]
        makers = [m for m in mentions[pid] if m["affiliated"]]
        cat = cats[p["category_id"]]
        sbm = json.loads(s["sentiment_by_month"])
        totals = Counter()
        for v in sbm.values():
            totals.update(v)
        v = verdicts.get(pid)
        good = [m for m in ms if m["stance"] == "recommend" and m["quote_verbatim"] and 50 <= len(m["reason_quote"]) <= 220]
        best = max(good, key=lambda m: (m["confidence"], len(m["reason_quote"])), default=None)
        why = (v["text"].split(". ")[0].rstrip(".") + ".") if v else (best["reason_quote"] if best else "")
        summary = {
            "slug": p["slug"], "name": p["name"], "category": cat["slug"], "category_name": cat["name"],
            "description": p["description"] or "", "recommenders": s["distinct_recommenders"],
            "commenters": s["distinct_commenters"], "warners": s["warners"], "score": s["weighted_score"],
            "rising": s["rising_score"], "first": s["first_mention"][:10], "latest": s["latest_mention"][:10],
            "why": why, "why_is_quote": not v and bool(best), "has_verdict": bool(v),
            "sentiment": {k: totals.get(k, 0) for k in ("recommend", "neutral", "compare", "warn")},
            "spark": [sbm.get(mo, {}).get("recommend", 0) for mo in MONTHS], "listed": s["distinct_recommenders"] >= MIN_LIST,
            "loved": [t["label"] for t in themes[pid] if t["polarity"] == "praise"][:2],
            "caveat": next((t["label"] for t in themes[pid] if t["polarity"] == "caveat"), ""),
            "short_quote": best["reason_quote"] if best else "",
        }
        summaries.append(summary)

        by_id = {m["id"]: m for m in mentions[pid]}
        verdict = None
        if v:
            verdict = [{"text": c["text"], "cites": [quote_obj(by_id[i]) for i in c["cites"] if i in by_id]}
                       for c in json.loads(v["clauses"])]
        cited = {q["id"] for c in verdict or [] for q in c["cites"]}
        th = {"praise": [], "caveat": []}
        for t in themes[pid]:
            th[t["polarity"]].append({"label": t["label"], "summary": t["summary"],
                                      "quotes": [quote_obj(by_id[i]) for i in json.loads(t["mention_ids"]) if i in by_id]})
        if not themes[pid]:
            # No synthesis yet: show the strongest verbatim quotes per stance, without labels.
            rec = [m for m in ms if m["stance"] == "recommend" and m["quote_verbatim"] and m["reason_quote"]][:5]
            warn = [m for m in ms if m["stance"] == "warn" and m["quote_verbatim"] and m["reason_quote"]][:4]
            if rec:
                th["praise"].append({"label": "", "summary": "", "quotes": [quote_obj(m) for m in rec]})
            if warn:
                th["caveat"].append({"label": "", "summary": "", "quotes": [quote_obj(m) for m in warn]})
        who, seen = [], set()
        for m in ms:
            if m["recommender_context"] and m["by"] not in seen and m["stance"] in ("recommend", "neutral"):
                seen.add(m["by"])
                who.append({"by": m["by"], "context": m["recommender_context"], "url": hn(m["item_id"]), "date": m["time"][:10]})
        tags = []
        if v:
            tags = [t["tag"] for t in json.loads(v["use_case_tags"] or "[]")]
        uc = Counter(m["use_case"].lower() for m in ms if m["use_case"])
        compared = []
        for q, co, sw_from, sw_to in sorted(comps[pid], key=lambda c: -(c[1] + 2 * (c[2] + c[3])))[:8]:
            if q in paged:
                compared.append({"slug": products[q]["slug"], "name": products[q]["name"], "co": co,
                                 "switched_from": sw_from, "switched_to": sw_to})
        threads = Counter()
        titles = {}
        for m in mentions[pid]:
            if m["story_id"]:
                threads[m["story_id"]] += 1
                titles[m["story_id"]] = m["story_title"]
        detail = {
            **summary,
            "domain": p["domain"], "github_repo": p["github_repo"], "official_url": p["official_url"] or (
                f"https://github.com/{p['github_repo'].split('/', 1)[1]}" if p["github_repo"] else (f"https://{p['domain']}" if p["domain"] else "")),
            "pricing": p["pricing"] or "", "aliases": [r[0] for r in con.execute("SELECT raw_name FROM alias WHERE product_id = ? ORDER BY n DESC", (pid,))],
            "verdict": verdict, "praise": th["praise"], "caveats": th["caveat"], "who": who[:12], "tags": tags,
            "use_cases": [u for u, _ in uc.most_common(6)], "compared": compared,
            "makers": [quote_obj(m) for m in makers[:6]],
            "threads": [{"url": hn(sid), "title": titles[sid] or f"item {sid}", "mentions": n} for sid, n in threads.most_common(6)],
            "sentiment_by_month": {mo: sbm.get(mo, {}) for mo in MONTHS},
            "quotes": [quote_obj(m) for m in ms if m in ms[:40] or m["id"] in cited],
        }
        (DATA / "products" / f"{p['slug']}.json").write_text(json.dumps(detail))

    summaries.sort(key=lambda s: -s["score"])
    home = home_data(con, summaries, mentions, products, themes, verdicts)
    cat_counts = Counter(s["category"] for s in summaries if s["listed"])
    stats = {
        "comments": duckdb.sql(f"SELECT count(DISTINCT id) FROM read_parquet('{ROOT}/data/raw/month=*/*.parquet', union_by_name=true) "
                               "WHERE type = 2 AND time >= '2025-11-01'").fetchall()[0][0],
        "candidates": duckdb.sql(f"SELECT count(*) FROM read_parquet('{ROOT}/data/candidates.parquet')").fetchall()[0][0],
        "labeled": con.execute("SELECT count(*) FROM hn_item").fetchone()[0],
        "mentions": con.execute("SELECT count(*) FROM mention").fetchone()[0],
        "products": len(summaries), "listed": sum(s["listed"] for s in summaries),
        "verdicts": len(verdicts), "window": "Nov 2025 to Oct 2026",
    }
    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stats": stats, "months": MONTHS,
        "categories": [{"slug": c["slug"], "name": c["name"], "count": cat_counts[c["slug"]]}
                       for c in sorted(cats.values(), key=lambda c: -cat_counts[c["slug"]]) if cat_counts[c["slug"]]],
        "products": summaries, "home": home,
    }
    (DATA / "catalog.json").write_text(json.dumps(catalog))
    listed = [s for s in summaries if s["listed"]]
    (EXPORT / "products.json").write_text(json.dumps({"generated_at": catalog["generated_at"], "stats": stats, "products": listed}, indent=1))
    (EXPORT / "p").mkdir()
    for f in (DATA / "products").glob("*.json"):
        shutil.copy(f, EXPORT / "p" / f.name)
    for c in catalog["categories"]:
        (EXPORT / f"category-{c['slug']}.json").write_text(json.dumps([s for s in listed if s["category"] == c["slug"]], indent=1))
    print(f"published {len(summaries):,} product pages ({stats['listed']:,} listed), {len(catalog['categories'])} categories, "
          f"{stats['verdicts']} verdicts")
    return 0


def best_quote(ms, other_items=None):
    """Highest-confidence verbatim recommend quote, preferring comments that also mention the other product."""
    good = [m for m in ms if m["stance"] == "recommend" and m["quote_verbatim"] and 40 <= len(m["reason_quote"]) <= 240
            and not m["affiliated"]]
    if other_items:
        both = [m for m in good if m["item_id"] in other_items]
        good = both or good
    m = max(good, key=lambda m: (m["confidence"], -abs(len(m["reason_quote"]) - 140)), default=None)
    return m and {"quote": m["reason_quote"], "by": m["by"], "date": m["time"][:10], "url": hn(m["item_id"])}


def home_data(con, summaries, mentions, products, themes, verdicts):
    by_slug = {s["slug"]: s for s in summaries}
    listed = {p["slug"]: pid for pid, p in products.items() if p["slug"] in by_slug and by_slug[p["slug"]]["listed"]}
    # Pick of the day pool: products with a verdict and both a praise and a caveat quote.
    picks = []
    for pid, v in verdicts.items():
        th = themes[pid]
        pq = next((t for t in th if t["polarity"] == "praise"), None)
        cq = next((t for t in th if t["polarity"] == "caveat"), None)
        if not (pq and cq) or products[pid]["slug"] not in by_slug:
            continue
        ids = {m["id"]: m for m in mentions[pid]}

        def q(t):
            for i in json.loads(t["mention_ids"]):
                m = ids.get(i)
                if m and m["quote_verbatim"] and m["reason_quote"]:
                    return {"label": t["label"], "quote": m["reason_quote"], "by": m["by"], "date": m["time"][:10], "url": hn(m["item_id"])}
            return None
        praise, caveat = q(pq), q(cq)
        if praise and caveat:
            s = by_slug[products[pid]["slug"]]
            picks.append({"slug": s["slug"], "name": s["name"], "category_name": s["category_name"], "category": s["category"],
                          "description": s["description"], "recommenders": s["recommenders"], "verdict": v["text"],
                          "praise": praise, "caveat": caveat})
    picks.sort(key=lambda p: p["slug"])
    (EXPORT / "picks.json").write_text(json.dumps(picks))

    # Debates: frequently co-mentioned listed pairs with verdicts on both sides.
    items_of = {pid: {m["item_id"] for m in ms} for pid, ms in mentions.items()}
    rows = con.execute("""SELECT product_a, product_b, co_mentions, switched_a_to_b, switched_b_to_a FROM comparison
                          ORDER BY co_mentions + 3 * (switched_a_to_b + switched_b_to_a) DESC LIMIT 400""").fetchall()
    debates, used, n_ai = [], set(), 0
    for a, b, co, ab, ba in rows:
        if a not in verdicts or b not in verdicts or a in used or b in used:
            continue
        sa, sb = by_slug.get(products[a]["slug"]), by_slug.get(products[b]["slug"])
        if not (sa and sb and sa["listed"] and sb["listed"]):
            continue
        is_ai = {sa["category"], sb["category"]} & {"ai", "ai-coding"}
        if is_ai and n_ai >= 2:
            continue
        qa = best_quote(mentions[a], items_of[b])
        qb = best_quote([m for m in mentions[b] if not qa or hn(m["item_id"]) != qa["url"]], items_of[a])
        if not (qa and qb):
            continue
        n_ai += bool(is_ai)
        used |= {a, b}
        debates.append({"co": co, "sides": [
            {"slug": sa["slug"], "name": sa["name"], "recommenders": sa["recommenders"], "warners": sa["warners"], "switched_in": ba, "quote": qa},
            {"slug": sb["slug"], "name": sb["name"], "recommenders": sb["recommenders"], "warners": sb["warners"], "switched_in": ab, "quote": qb}]})
        if len(debates) == 4:
            break

    # Hidden gems: a few recommenders, no warnings, one per category.
    gems, cats_seen = [], set()
    for s in sorted(summaries, key=lambda s: -s["score"]):
        if 3 <= s["recommenders"] <= 8 and s["warners"] == 0 and s["category"] not in cats_seen and s["short_quote"] \
                and s["category"] not in ("ai", "ai-coding", "other"):
            gems.append(s["slug"])
            cats_seen.add(s["category"])
        if len(gems) == 6:
            break
    return {"debates": debates, "gems": gems, "pick_count": len(picks)}


if __name__ == "__main__":
    sys.exit(main())
