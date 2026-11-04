"""Stage 6: per-product synthesis with Opus (themes, two-sentence verdict, use-case tags).

Only products with at least --min recommenders. Each run is cached in data/synth/synth_v1/<product slug>.json
keyed by the product's mention set; a validator drops any clause or theme that cites an unknown mention id,
and the product is retried once if the verdict fails validation. Stops cleanly on a usage limit.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hp.sqlite"
VERSION = "synth_v1"
CACHE = ROOT / "data" / "synth" / VERSION
MAX_MENTIONS = 60


def pick_mentions(con, pid, limit=None):
    rows = con.execute("""
        SELECT m.id, m.stance, m.reason_quote, m.recommender_context, m.use_case, m.switched_from, i.by, i.time, i.text
        FROM mention m JOIN hn_item i ON i.id = m.item_id
        WHERE m.product_id = ? AND m.affiliated = 0
        ORDER BY (m.stance IN ('recommend', 'warn')) DESC, m.quote_verbatim DESC, i.time DESC, m.id
    """, (pid,)).fetchall()
    out, users = [], {}
    for r in rows:
        users[r[6]] = users.get(r[6], 0) + 1
        if users[r[6]] <= 2:
            out.append(r)
    return out[:limit or MAX_MENTIONS]


def mention_hash(mentions):
    return hashlib.sha1(json.dumps(sorted((r[0], r[1], r[2]) for r in mentions)).encode()).hexdigest()[:12]


def fmt(r):
    text = " ".join((r[8] or "").split())[:600]
    extra = []
    if r[3]:
        extra.append(f"context=\"{r[3]}\"")
    if r[5]:
        extra.append(f"switched_from=\"{r[5]}\"")
    return f"[m{r[0]}] {r[7][:7]} stance={r[1]} {' '.join(extra)}\n  quote: \"{r[2]}\"\n  comment: \"{text}\""


def valid_ids(xs, ids):
    out = []
    for x in xs or []:
        s = str(x).lstrip("m")
        if s.isdigit() and int(s) in ids:
            out.append(int(s))
    return out


def validate(out, ids):
    errors = []
    clauses = []
    for c in out.get("verdict") or []:
        cites = valid_ids(c.get("cites"), ids)
        if not cites:
            errors.append(f"verdict clause has no valid citation: {c.get('text')!r}")
        clauses.append({"text": c.get("text", ""), "cites": cites})
    if not clauses:
        errors.append("missing verdict")

    def themes(key):
        res = []
        for t in out.get(key) or []:
            q = valid_ids(t.get("quotes"), ids)
            if q and t.get("label"):
                res.append({"label": t["label"], "summary": t.get("summary", ""), "quotes": q[:3]})
        return res

    tags = []
    for t in out.get("use_case_tags") or []:
        c = valid_ids(t.get("cites"), ids)
        if c and t.get("tag"):
            tags.append({"tag": t["tag"], "cites": c})
    clean = {"description": out.get("description", ""), "official_url": out.get("official_url", ""),
             "pricing": out.get("pricing", "unknown"), "verdict": [c for c in clauses if c["cites"]],
             "praise": themes("praise"), "caveats": themes("caveats"), "use_case_tags": tags}
    return clean, errors


def parse_obj(text):
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0]
    i, j = s.find("{"), s.rfind("}")
    return json.loads(s[i:j + 1])


COST = []  # $ per model call this run (from claude -p total_cost_usd)


def synth_one(prod, mentions, template):
    pid, slug, name, cat = prod
    ids = {r[0] for r in mentions}
    prompt = template.replace("{name}", name).replace("{category}", cat) + "\n".join(fmt(r) for r in mentions)
    errors = []
    for attempt in range(2):
        p = prompt if not errors else prompt + "\n\nYour previous answer was rejected: " + "; ".join(errors[:5]) + \
            ". Fix it: every verdict clause must cite ids from the list above."
        text, usage = llm.run(p, model="opus", effort="medium",
                          system="You are a careful editor who never states anything the sources do not support.")
        COST.append(usage.get("cost_usd", 0.0))
        try:
            out = parse_obj(text)
        except (ValueError, json.JSONDecodeError):
            errors = ["output was not valid JSON"]
            continue
        clean, errors = validate(out, ids)
        if not errors:
            break
    clean["rejected"] = errors
    return clean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=5, help="minimum distinct recommenders")
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-mentions", type=int, default=MAX_MENTIONS, help="quotes given to the model per product")
    ap.add_argument("--order", choices=["score", "recommenders"], default="score")
    ap.add_argument("--export", help="write the prompts' inputs to this folder (no usernames) instead of calling a model")
    ap.add_argument("--budget", type=float, default=0, help="stop starting new products after this many USD")
    ap.add_argument("--load-only", action="store_true", help="load cached verdicts into hp.sqlite without calling a model")
    ap.add_argument("--inputs", help="synthesize from an --export folder instead of data/hp.sqlite (e.g. in the cloud)")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    template = (ROOT / "prompts" / f"{VERSION}.md").read_text()
    con = None
    if args.inputs:
        exported = [json.loads(line) for line in (Path(args.inputs) / "products.jsonl").open()]
        prods = [tuple(e["product"]) for e in exported]
        picked = {e["product"][0]: [tuple(m) for m in e["mentions"]] for e in exported}
        args.max_mentions = exported[0]["max_mentions"] if exported else args.max_mentions
    else:
        con = sqlite3.connect(DB)
        order = "s.weighted_score" if args.order == "score" else "s.distinct_recommenders"
        prods = con.execute(f"""
            SELECT p.id, p.slug, p.name, c.name FROM product p JOIN product_score s ON s.product_id = p.id
            JOIN category c ON c.id = p.category_id
            WHERE s.distinct_recommenders >= ? ORDER BY {order} DESC LIMIT ?
        """, (args.min, args.limit)).fetchall()
        picked = {p[0]: pick_mentions(con, p[0], args.max_mentions) for p in prods}
    if args.load_only:
        load(con, prods)
        return 0
    if args.export:
        return export(Path(args.export), prods, picked, args.max_mentions, template)
    jobs = []
    for prod in prods:
        mentions = picked[prod[0]]
        h = mention_hash(mentions)
        f = CACHE / f"{prod[1]}.json"
        if f.exists() and json.loads(f.read_text()).get("mention_hash") == h:
            continue
        jobs.append((prod, mentions, h, f))
    print(f"synthesis: {len(prods)} products eligible (>= {args.min} recommenders), {len(jobs)} to (re)generate", flush=True)
    done = 0
    with ThreadPoolExecutor(args.workers) as ex:
        def guarded(prod, ms):
            if args.budget and sum(COST) >= args.budget:
                raise RuntimeError("budget reached, skipped")
            return synth_one(prod, ms, template)
        futs = {ex.submit(guarded, prod, ms): (prod, h, f) for prod, ms, h, f in jobs}
        try:
            for fut in as_completed(futs):
                prod, h, f = futs[fut]
                try:
                    out = fut.result()
                except RuntimeError as e:
                    print(f"  {prod[2]}: failed ({e})", flush=True)
                    continue
                out.update({"mention_hash": h, "product_id": prod[0], "version": VERSION, "max_mentions": args.max_mentions,
                            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                f.write_text(json.dumps(out, indent=1))
                done += 1
                flag = f" (rejected: {out['rejected'][:1]})" if out["rejected"] else ""
                print(f"  {done}/{len(jobs)} {prod[2]}: {len(out['praise'])} praise, {len(out['caveats'])} caveats{flag}, "
                      f"${sum(COST):.2f}", flush=True)
        except llm.UsageLimit as e:
            for fu in futs:
                fu.cancel()
            print(f"STOPPED: usage limit ({e}). Re-run to resume; finished products are cached.", flush=True)
            return 2
    if con:
        load(con, prods)
    return 0


def export(folder, prods, picked, max_mentions, template):
    """Everything synth_one needs, per product, for running synthesis elsewhere. Usernames are dropped;
    the prompt only ever shows mention ids, dates, stances, quotes and comment text."""
    folder.mkdir(parents=True, exist_ok=True)
    sizes = []
    with (folder / "products.jsonl").open("w") as f:
        for prod in prods:
            ms = [[r[0], r[1], r[2], r[3], r[4], r[5], None, r[7], " ".join((r[8] or "").split())[:700]]
                  for r in picked[prod[0]]]
            f.write(json.dumps({"product": list(prod), "max_mentions": max_mentions, "mentions": ms}) + "\n")
            prompt = template.replace("{name}", prod[2]).replace("{category}", prod[3]) + "\n".join(fmt(r) for r in ms)
            sizes.append(len(prompt) / 4)
    (folder / f"{VERSION}.md").write_text(template)
    print(f"exported {len(prods)} products to {folder}; prompt ~{sum(sizes) / max(1, len(sizes)):,.0f} tokens on average "
          f"(max {max(sizes, default=0):,.0f}), ~{sum(sizes) / 1e6:.1f}M input tokens in total")
    return 0


def load(con, prods, keep_stale=True):
    """Write cached synthesis into theme/verdict and fill product metadata.

    A verdict written from an older quote set is still loaded when every mention it cites still belongs to
    the product (it was written from fewer quotes but every claim stays traceable); otherwise it is dropped."""
    con.execute("DELETE FROM theme")
    con.execute("DELETE FROM verdict")
    n = stale = 0
    for pid, slug, name, _ in prods:
        f = CACHE / f"{slug}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text())
        if d.get("mention_hash") != mention_hash(pick_mentions(con, pid, d.get("max_mentions", MAX_MENTIONS))):
            cited = {i for c in d.get("verdict", []) for i in c["cites"]} | \
                    {i for k in ("praise", "caveats") for t in d.get(k, []) for i in t["quotes"]}
            owned = {r[0] for r in con.execute("SELECT id FROM mention WHERE product_id = ? AND affiliated = 0", (pid,))}
            if not keep_stale or not d.get("verdict") or not cited <= owned:
                continue
            stale += 1
        for pol, key in (("praise", "praise"), ("caveat", "caveats")):
            for t in d[key]:
                con.execute("INSERT INTO theme(product_id, polarity, label, summary, mention_ids, synth_version) VALUES (?,?,?,?,?,?)",
                            (pid, pol, t["label"], t["summary"], json.dumps(t["quotes"]), d["version"]))
        if d["verdict"]:
            cited = sorted({i for c in d["verdict"] for i in c["cites"]})
            con.execute("INSERT INTO verdict VALUES (?,?,?,?,?,?,?)",
                        (pid, "".join(c["text"] for c in d["verdict"]).strip(), json.dumps(d["verdict"]),
                         json.dumps(cited), json.dumps(d["use_case_tags"]), d["version"], d["generated_at"]))
            n += 1
        con.execute("UPDATE product SET description = ?, official_url = ?, pricing = ? WHERE id = ?",
                    (d["description"], d["official_url"], d["pricing"], pid))
    con.commit()
    print(f"loaded {n} verdicts into hp.sqlite ({stale} from an older quote set, all citations still valid)")


if __name__ == "__main__":
    sys.exit(main())
