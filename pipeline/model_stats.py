"""Export AI model comparison numbers as aggregates (no comment text, no usernames) to models/model_stats.json."""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from compare import BUCKETS, Ctx, STANCES, buckets_of, monthly, people_stance  # noqa: E402
from publish import DB, MONTHS  # noqa: E402
from resolve import name_key  # noqa: E402


def load():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cats = {r["id"]: dict(r) for r in con.execute("SELECT * FROM category")}
    products = {r["id"]: dict(r) for r in con.execute("SELECT * FROM product")}
    mentions = defaultdict(list)
    for r in con.execute("""SELECT m.*, i.by, i.time FROM mention m JOIN hn_item i ON i.id = m.item_id"""):
        mentions[r["product_id"]].append(dict(r))
    alias = {}
    for pid, raw in con.execute("SELECT product_id, raw_name FROM alias ORDER BY n"):
        alias[name_key(raw)] = pid
    return Ctx(products, cats, mentions, alias)


def main(out):
    ctx = load()
    releases = []
    for r in ctx.releases():
        a, w = r["all_time"], r["in_window"]
        releases.append({"family": r["family_name"], "line": r["line"], "version": r["version"], "slug": r["slug"], "label": r["label"],
                         "first_seen": r["first_seen"], "third_mention_date": r["third_mention_date"], "window_start": r["window_start"],
                         "window_end": r["window_end"], "current": r["current"], "people_total": a["people"],
                         "rec": a["recommend"], "neutral": a["neutral"], "compare": a["compare"], "warn": a["warn"],
                         "window": w and {"people": w["people"], "rec": w["recommend"], "neutral": w["neutral"], "compare": w["compare"], "warn": w["warn"]}})
    releases.sort(key=lambda r: (r["family"], r["line"], -float(r["version"].rstrip("B"))))
    listed = [f for f in ctx.top if len({m["by"] for m in ctx.fam_ms[f] if m["stance"] == "recommend"}) >= 3]
    latest = {}
    for r in releases:
        if r["current"] and (r["family"] not in latest or float(r["version"]) > float(latest[r["family"]]["version"])
                             or (float(r["version"]) == float(latest[r["family"]]["version"]) and r["people_total"] > latest[r["family"]]["people_total"])):
            latest[r["family"]] = r
    families = []
    for f in listed:
        ms = ctx.fam_ms[f]
        use = defaultdict(set)
        for m in ms:
            for b in buckets_of(m):
                use[b].add(m["by"])
        c = people_stance(ms)
        families.append({"family": ctx.name(f), "slug": ctx.slug(f), "people": c["people"],
                         "recommenders": len({m["by"] for m in ms if m["stance"] == "recommend"}),
                         "rec": c["recommend"], "warn": c["warn"], "neutral": c["neutral"], "compare": c["compare"],
                         "latest_release": latest.get(ctx.name(f), {}).get("label"),
                         "use_buckets": {BUCKETS[k][0]: len(v) for k, v in sorted(use.items(), key=lambda kv: -len(kv[1]))}})
    month_rows = {"months": MONTHS, "families": {ctx.name(f): monthly(ctx.fam_ms[f]) for f in listed},
                  "lines": {ctx.products[p]["name"]: monthly([m for m in ctx.mentions[p] if not m["affiliated"]])
                            for p in sorted({ctx.products_by_slug(r["slug"]) for r in releases})}}
    pairs = ctx.pairs(listed)
    pair_rows = [{"a": ctx.name(a), "b": ctx.name(b), "comments": v["comments"], "a_preferred": v["a"], "b_preferred": v["b"],
                  "unclear": v["comments"] - v["a"] - v["b"]}
                 for (a, b), v in sorted(pairs.items(), key=lambda kv: -kv[1]["comments"])[:20]]
    switches = [{"from": ctx.name(a), "to": ctx.name(b), "people": len(who)}
                for (a, b), who in sorted(ctx.switches().items(), key=lambda kv: -len(kv[1]))]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"window": "Nov 2025 to Oct 2026", "stance_note": "distinct people, each person's latest stance in that scope",
                                     "releases": releases, "monthly": month_rows, "families": families, "pairs": pair_rows, "switches": switches}, indent=1))
    print(f"{len(releases)} releases, {len(families)} families, {len(pair_rows)} pairs, {len(switches)} flows ({sum(s['people'] for s in switches)} people-flows)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
