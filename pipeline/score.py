"""Stage 5: score products and build the comparison table in data/hp.sqlite.

One vote per HN user per product (their latest stance counts), affiliated mentions excluded,
recency half-life of 3 years, warnings subtract. Rising = recommenders in the last 90 days
minus the trailing-year rate for 90 days.
"""
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from resolve import name_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hp.sqlite"
NOW = datetime(2026, 11, 1)
HALF_LIFE_YEARS = 3.0
STANCE_W = {"recommend": 1.0, "neutral": 0.3, "compare": 0.2, "warn": -1.0}


def main():
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT m.id, m.product_id, m.stance, m.affiliated, m.alternatives, m.switched_from, i.by, i.time, i.id
        FROM mention m JOIN hn_item i ON i.id = m.item_id
    """).fetchall()
    affiliated_users = {(r[1], r[6]) for r in rows if r[3]}
    by_product = defaultdict(list)
    for r in rows:
        if (r[1], r[6]) in affiliated_users:
            continue
        by_product[r[1]].append(r)

    con.execute("DELETE FROM product_score")
    cat = dict(con.execute("SELECT id, category_id FROM product"))
    for pid, ms in by_product.items():
        latest = {}
        for r in sorted(ms, key=lambda r: r[7]):
            latest[r[6]] = r
        recommenders = {r[6] for r in ms if r[2] == "recommend"}
        warners = {u for u, r in latest.items() if r[2] == "warn"}
        score = 0.0
        for u, r in latest.items():
            age = (NOW - datetime.fromisoformat(r[7])).days / 365.25
            score += STANCE_W[r[2]] * 0.5 ** (age / HALF_LIFE_YEARS)
        recent = {r[6] for r in ms if r[2] == "recommend" and (NOW - datetime.fromisoformat(r[7])).days <= 90}
        prior = {r[6] for r in ms if r[2] == "recommend" and 90 < (NOW - datetime.fromisoformat(r[7])).days <= 365}
        rising = len(recent) - len(prior) * 90 / 275
        by_month = defaultdict(Counter)
        by_year = defaultdict(Counter)
        seen = set()
        for r in ms:
            k = (r[6], r[7][:7])
            if k in seen:
                continue
            seen.add(k)
            by_month[r[7][:7]][r[2]] += 1
            by_year[r[7][:4]][r[2]] += 1
        times = sorted(r[7] for r in ms)
        con.execute("INSERT INTO product_score VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, cat.get(pid), len(recommenders - warners) + len(recommenders & warners), len(latest),
                     len(warners), round(score, 3), round(rising, 2), times[0], times[-1],
                     json.dumps({k: dict(v) for k, v in sorted(by_month.items())}),
                     json.dumps({k: dict(v) for k, v in sorted(by_year.items())})))

    # Comparisons: products named together in one comment, alternatives lists, and switched-from pairs.
    alias = {}
    for pid, raw in con.execute("SELECT product_id, raw_name FROM alias ORDER BY n"):
        alias[name_key(raw)] = pid
    co = Counter()
    switched = Counter()
    per_item = defaultdict(set)
    for r in rows:
        per_item[r[8]].add(r[1])
        for alt in json.loads(r[4] or "[]"):
            q = alias.get(name_key(str(alt)))
            if q and q != r[1]:
                per_item[r[8]].add(q)
        if r[5]:
            q = alias.get(name_key(r[5]))
            if q and q != r[1]:
                switched[(q, r[1])] += 1
    for pids in per_item.values():
        for a, b in combinations(sorted(pids), 2):
            co[(a, b)] += 1
    con.execute("DELETE FROM comparison")
    keys = set(co) | {tuple(sorted(k)) for k in switched}
    con.executemany("INSERT INTO comparison VALUES (?,?,?,?,?)",
                    [(a, b, co.get((a, b), 0), switched.get((a, b), 0), switched.get((b, a), 0)) for a, b in keys])
    con.commit()
    top = con.execute("""SELECT p.name, s.distinct_recommenders, s.warners, s.weighted_score, s.rising_score
                         FROM product_score s JOIN product p ON p.id = s.product_id
                         ORDER BY s.weighted_score DESC LIMIT 15""").fetchall()
    n = con.execute("SELECT count(*) FROM product_score WHERE distinct_recommenders >= 3").fetchone()[0]
    print(f"scored {len(by_product):,} products; {n:,} with 3+ recommenders; {len(keys):,} comparison pairs")
    for t in top:
        print(f"  {t[0]}: {t[1]} recommenders, {t[2]} warn, score {t[3]}, rising {t[4]:+}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
