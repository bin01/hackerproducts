"""Family-wide themes and verdict for AI model families with tiers (Claude across Opus, Sonnet, Haiku, ...).

Uses the same prompt, model and citation validator as synth.py. Quotes are sampled across a family's tiers in proportion
to their people. Cached in data/synth/family_v1/<slug>.json, keyed by the sampled mention set.
"""
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from publish import family_parents  # noqa: E402
from synth import DB, MAX_MENTIONS, ROOT, mention_hash, pick_mentions, synth_one  # noqa: E402
import synth  # noqa: E402

CACHE = ROOT / "data" / "synth" / "family_v1"
TOP_FAMILIES = 11


def sample(con, members):
    """Quotes per tier in proportion to its people, at least 3 from each tier that has any."""
    picked = {m: pick_mentions(con, m, MAX_MENTIONS) for m in members}
    weight = {m: con.execute("SELECT count(DISTINCT i.by) FROM mention x JOIN hn_item i ON i.id = x.item_id WHERE x.product_id = ? AND x.affiliated = 0",
                             (m,)).fetchone()[0] for m in members}
    total = sum(weight.values()) or 1
    out, seen = [], set()
    for m in sorted(members, key=lambda m: -weight[m]):
        take = max(3, round(MAX_MENTIONS * weight[m] / total))
        for r in picked[m][:take]:
            if r[0] not in seen:
                seen.add(r[0])
                out.append(r)
    return out[:MAX_MENTIONS]


def main():
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT id, slug, name, category_id FROM product").fetchall()
    cats = {r[0]: {"id": r[0], "slug": r[1], "name": r[2]} for r in con.execute("SELECT id, slug, name FROM category")}
    products = {r[0]: {"slug": r[1], "name": r[2], "category_id": r[3]} for r in rows}
    parents = family_parents(products, cats)
    tiers = {}
    for child, parent in parents.items():
        tiers.setdefault(parent, []).append(child)
    def people(pid):
        return con.execute("SELECT count(DISTINCT i.by) FROM mention x JOIN hn_item i ON i.id = x.item_id WHERE x.affiliated = 0 AND x.stance = 'recommend' AND x.product_id IN (%s)"
                           % ",".join(str(q) for q in [pid, *tiers[pid]])).fetchone()[0]
    top = sorted(tiers, key=people, reverse=True)[:TOP_FAMILIES]
    template = (ROOT / "prompts" / f"{synth.VERSION}.md").read_text()
    CACHE.mkdir(parents=True, exist_ok=True)
    jobs = []
    for pid in top:
        ms = sample(con, [pid, *tiers[pid]])
        f = CACHE / f"{products[pid]['slug']}.json"
        h = mention_hash(ms)
        if f.exists() and json.loads(f.read_text()).get("mention_hash") == h:
            continue
        jobs.append((pid, ms, f, h))
    print(f"family synthesis: {len(top)} families, {len(jobs)} to generate: {[products[j[0]]['slug'] for j in jobs]}", flush=True)

    def run(job):
        pid, ms, f, h = job
        p = products[pid]
        out = synth_one((pid, p["slug"], f"{p['name']} (all its tiers and versions)", "AI models"), ms, template)
        out.update(mention_hash=h, generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        f.write_text(json.dumps(out, indent=1))
        print(f"  {p['slug']}: {len(out['verdict'])} clauses, {len(out['praise'])} praise, {len(out['caveats'])} caveats, rejected={out['rejected']}", flush=True)
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(run, jobs))
    print(f"calls: {len(synth.COST)}", flush=True)


if __name__ == "__main__":
    main()
