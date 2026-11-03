"""Stage 3: LLM extraction of product mentions from candidate comments.

Writes data/extract/<prompt>/<model>-<effort>/batch-<first id>.jsonl, one line per input comment.
Resumable: comment ids already present in that folder are skipped. Stops cleanly on a usage limit.

  uv run pipeline/extract.py --model haiku --effort low --top 20000
  uv run pipeline/extract.py --model sonnet --effort low --ids data/gold/ids.txt
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CANDS = ROOT / "data" / "candidates.parquet"


def out_dir(prompt, model, effort):
    return ROOT / "data" / "extract" / prompt / f"{model}-{effort}"


def done_ids(d):
    ids = set()
    for f in d.glob("*.jsonl"):
        for line in f.open():
            ids.add(json.loads(line)["id"])
    return ids


def norm_ws(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def fmt(r):
    parts = [f"=== id={r['id']}"]
    if r["story_title"]:
        parts.append(f"story: {r['story_title']}")
    if r["parent_text"]:
        parts.append(f"parent comment: {r['parent_text'][:400]}")
    parts.append(f"comment: {r['text'][:3000]}")
    return "\n".join(parts)


STANCE = {"r": "recommend", "n": "neutral", "w": "warn", "c": "compare"}


def expand(p):
    """Map the compact extract_v2 product record onto the extract_v1 field names."""
    if "name" in p:
        return p
    return {"name": p.get("n", ""), "url": p.get("u", ""), "stance": STANCE.get(p.get("s"), "neutral"),
            "reason_quote": p.get("q", ""), "use_case": p.get("uc", ""), "alternatives": p.get("alt", []),
            "switched_from": p.get("sf", ""), "recommender_context": p.get("ctx", ""),
            "affiliated": bool(p.get("aff")), "category": p.get("cat", ""), "confidence": p.get("cf", 0.5)}


def run_batch(rows, prompt_text, args, d):
    body = prompt_text + "\n\n".join(fmt(r) for r in rows)
    text, usage = llm.run(body, model=args.model, effort=args.effort, thinking=not args.no_thinking)
    got = {}
    done = None
    for o in llm.parse_jsonl(text):
        if "done" in o:
            done = o["done"]
            continue
        try:
            got[int(o["id"])] = [expand(p) for p in o["p"]] if "p" in o else (o.get("products") or [])
        except (KeyError, TypeError, ValueError):
            continue
    if done is not None:
        # Compact prompts list only comments with products; a completed reply means the rest have none.
        for r in rows:
            got.setdefault(r["id"], [])
    by_id = {r["id"]: r for r in rows}
    out = []
    for r in rows:
        prods = got.get(r["id"])
        rec = {"id": r["id"], "model": args.model, "effort": args.effort, "prompt": args.prompt,
               "missing": prods is None, "products": []}
        for p in prods or []:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            q = p.get("reason_quote") or ""
            p["quote_verbatim"] = bool(q) and norm_ws(q) in norm_ws(by_id[r["id"]]["text"])
            rec["products"].append(p)
        out.append(rec)
    with open(d / f"batch-{rows[0]['id']}.jsonl", "w") as f:
        for rec in out:
            f.write(json.dumps(rec) + "\n")
    return len(rows), sum(len(o["products"]) for o in out), sum(o["missing"] for o in out), usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--prompt", default="extract_v1")
    ap.add_argument("--top", type=int, default=0, help="extract the N highest-priority candidates")
    ap.add_argument("--ids", help="file with one comment id per line")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-thinking", action="store_true", help="disable extended thinking (cheaper output)")
    ap.add_argument("--scores", help="data/unlabeled_scores.npy: pick by classifier score instead of priority")
    ap.add_argument("--min-score", type=float, default=0.5)
    args = ap.parse_args()

    d = out_dir(args.prompt, args.model, args.effort)
    d.mkdir(parents=True, exist_ok=True)
    prompt_text = (ROOT / "prompts" / f"{args.prompt}.md").read_text()
    done = done_ids(d)

    if args.ids:
        want = [int(x) for x in Path(args.ids).read_text().split()]
        q = f"SELECT * FROM read_parquet('{CANDS}') WHERE id IN ({','.join(map(str, want))})"
    elif args.scores:
        import numpy as np
        ids, sc = np.load(args.scores)
        keep = sorted(((s, int(i)) for i, s in zip(ids, sc) if s >= args.min_score), reverse=True)[: args.top or None]
        order = ",".join(str(i) for _, i in keep)
        q = f"SELECT c.* FROM read_parquet('{CANDS}') c JOIN (SELECT unnest([{order}]) AS id, generate_subscripts([{order}], 1) AS k) o USING (id) ORDER BY o.k"
    else:
        q = f"SELECT * FROM read_parquet('{CANDS}') ORDER BY priority DESC, time DESC LIMIT {args.top}"
    rel = duckdb.sql(q)
    cols = rel.columns
    rows = [dict(zip(cols, r)) for r in rel.fetchall()]
    todo = [r for r in rows if r["id"] not in done]
    print(f"{args.model}-{args.effort} {args.prompt}: {len(rows):,} selected, {len(rows) - len(todo):,} done, "
          f"{len(todo):,} to do", flush=True)
    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]

    t0 = time.time()
    n_c = n_p = n_miss = 0
    tok_in = tok_out = 0
    with ThreadPoolExecutor(args.workers) as ex:
        futs = [ex.submit(run_batch, b, prompt_text, args, d) for b in batches]
        try:
            for i, f in enumerate(as_completed(futs), 1):
                try:
                    c, p, m, u = f.result()
                except RuntimeError as e:
                    print(f"  batch {i}/{len(batches)} failed, will retry on the next run: {str(e)[:160]}", flush=True)
                    continue
                n_c, n_p, n_miss = n_c + c, n_p + p, n_miss + m
                tok_in += u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                tok_out += u.get("output_tokens", 0)
                print(f"  batch {i}/{len(batches)}: {n_c:,} comments, {n_p:,} products, {n_miss} missing, "
                      f"{tok_in / 1e6:.2f}M in / {tok_out / 1e6:.2f}M out, {time.time() - t0:.0f}s", flush=True)
        except llm.UsageLimit as e:
            for f in futs:
                f.cancel()
            print(f"STOPPED: usage limit reached ({e}). Re-run the same command to resume.", flush=True)
            return 2
    print(f"done: {n_c:,} comments, {n_p:,} product mentions", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
