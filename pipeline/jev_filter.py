"""Score comments with TypeSafe's Jev model: probability that a comment names a software product with a stance.

Experimental alternative to the local yield classifier (pipeline/yield_model.py). The API key is read from
~/.config/typesafe/key and never logged. No usernames are sent. Results go to data/jev/scores.jsonl (resumable).

  uv run pipeline/jev_filter.py --sample 5000
"""
import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

import duckdb
import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "jev" / "scores.jsonl"
URL = "https://api.typesafe.ai/v1/systemone"
QUESTION = {
    "type": "noul",
    "instructions": "Does this comment name a specific software product and express a stance on it "
                    "(uses it, recommends it, warns against it, or compares it with alternatives)?",
    "criteria": {
        "true": "Names at least one specific app, tool, library, service, OS, browser or AI model and says something "
                "about using, liking, disliking or choosing it",
        "false": "Names no specific software product, only mentions one in passing, or is about hardware, books, "
                 "companies in general, people or programming languages",
    },
}


def state(r):
    s = {"comment": r["text"][:3000]}
    if r["story_title"]:
        s["thread_title"] = r["story_title"]
    if r["parent_text"]:
        s["replying_to"] = r["parent_text"][:400]
    return s


async def score(client, key, r, sem):
    body = {"model": "jev-latest", "state": state(r), "questions": {"has_product_stance": QUESTION}}
    async with sem:
        for attempt in range(4):
            t0 = time.time()
            try:
                resp = await client.post(URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=60)
                if resp.status_code == 429 or resp.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                d = resp.json()
                return {"id": r["id"], "p": d["answers"]["has_product_stance"]["noul"], "model": d.get("model"),
                        "usage": d.get("usage", {}), "ms": int((time.time() - t0) * 1000)}
            except httpx.HTTPError as e:
                err = type(e).__name__
                await asyncio.sleep(2 ** attempt)
        return {"id": r["id"], "error": err if "err" in dir() else "retries exhausted"}


async def main_async(rows, concurrency, max_input_tokens):
    key = (Path.home() / ".config" / "typesafe" / "key").read_text().strip()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(line)["id"] for line in OUT.open()} if OUT.exists() else set()
    todo = [r for r in rows if r["id"] not in done]
    print(f"jev: {len(rows):,} selected, {len(done):,} already scored, {len(todo):,} to do", flush=True)
    sem = asyncio.Semaphore(concurrency)
    used_in = used_out = n = errors = 0
    t0 = time.time()
    async with httpx.AsyncClient() as client:
        with OUT.open("a") as f:
            for start in range(0, len(todo), 200):
                res = await asyncio.gather(*(score(client, key, r, sem) for r in todo[start:start + 200]))
                for x in res:
                    if "error" in x:
                        errors += 1
                        continue
                    f.write(json.dumps(x) + "\n")
                    n += 1
                    used_in += x["usage"].get("input_tokens", 0)
                    used_out += x["usage"].get("output_tokens", 0)
                f.flush()
                print(f"  {n:,} scored, {errors} errors, {used_in / 1e6:.2f}M in / {used_out / 1e6:.3f}M out tokens, "
                      f"{time.time() - t0:.0f}s", flush=True)
                if used_in > max_input_tokens:
                    print(f"STOPPED: input-token budget {max_input_tokens:,} reached", flush=True)
                    return 2
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=20, help="random sample of already-labeled comments")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--max-input-tokens", type=int, default=5_000_000)
    args = ap.parse_args()
    lab = set()
    for f in (ROOT / "data" / "extract" / "extract_v1" / "sonnet-low").glob("*.jsonl"):
        for line in f.open():
            r = json.loads(line)
            if not r["missing"]:
                lab.add(r["id"])
    rel = duckdb.sql(f"SELECT id, text, story_title, parent_text FROM read_parquet('{ROOT}/data/candidates.parquet')")
    rows = [dict(zip(rel.columns, r)) for r in rel.fetchall() if r[0] in lab]
    random.Random(7).shuffle(rows)
    return asyncio.run(main_async(rows[: args.sample], args.concurrency, args.max_input_tokens))


if __name__ == "__main__":
    sys.exit(main())
