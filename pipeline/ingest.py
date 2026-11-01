"""Stage 1: ingest one year of HN items into data/raw/month=YYYY-MM/items.parquet.

Monthly files from the open-index/hacker-news dump on Hugging Face; months the dump
does not have yet come from the Algolia HN Search API. Resumable: finished months skip.
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
SRC = ROOT / "data" / "src"
HF = "https://huggingface.co/datasets/open-index/hacker-news/resolve/main/data/{y}/{y}-{m:02d}.parquet"
ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"

# Window: the 12 most recent full months, plus one earlier month so parent chains resolve.
END = (2026, 10)
MONTHS = 13

COLS = ["id", "type", "by", "time", "text", "parent", "url", "score", "title", "descendants"]
SCHEMA = pa.schema([
    ("id", pa.uint32()), ("type", pa.int8()), ("by", pa.string()), ("time", pa.timestamp("us")),
    ("text", pa.string()), ("parent", pa.uint32()), ("url", pa.string()), ("score", pa.int32()),
    ("title", pa.string()), ("descendants", pa.int32()), ("story_id", pa.uint32()),
])


def months():
    y, m = END
    out = []
    for _ in range(MONTHS):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(out)


def hf_month(client, y, m, dest):
    src = SRC / f"{y}-{m:02d}.parquet"
    if not src.exists():
        url = HF.format(y=y, m=m)
        r = client.head(url, follow_redirects=True)
        if r.status_code != 200:
            return False
        tmp = src.with_suffix(".part")
        with client.stream("GET", url, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.rename(src)
    duckdb.sql(f"""
        COPY (
          SELECT id::UINTEGER id, type::TINYINT type, "by", time::TIMESTAMP time, text,
                 parent::UINTEGER parent, url, score::INTEGER score, title, descendants::INTEGER descendants,
                 NULL::UINTEGER story_id
          FROM read_parquet('{src}')
          WHERE deleted = 0 AND dead = 0 AND type IN (1, 2)
        ) TO '{dest}' (FORMAT parquet, COMPRESSION zstd)
    """)
    src.unlink()
    return True


def algolia_month(client, y, m, dest):
    start = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = int(datetime(ny, nm, 1, tzinfo=timezone.utc).timestamp())
    return algolia_range(client, start, end, dest, f"{y}-{m:02d}")


def algolia_range(client, start, end, dest, label):
    rows = {}
    for tag in ("story", "comment"):
        hi = end
        while hi > start:
            params = {"tags": tag, "hitsPerPage": 1000,
                      "numericFilters": f"created_at_i>={start},created_at_i<{hi}"}
            for attempt in range(6):
                try:
                    r = client.get(ALGOLIA, params=params, timeout=60)
                    r.raise_for_status()
                    break
                except httpx.HTTPError:
                    time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f"algolia failed at {hi}")
            hits = r.json()["hits"]
            if not hits:
                break
            for h in hits:
                rows[int(h["objectID"])] = {
                    "id": int(h["objectID"]),
                    "type": 1 if tag == "story" else 2,
                    "by": h.get("author"),
                    "time": datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc).replace(tzinfo=None),
                    "text": h.get("comment_text") if tag == "comment" else h.get("story_text"),
                    "parent": h.get("parent_id"),
                    "url": h.get("url") or "",
                    "score": h.get("points"),
                    "title": h.get("title") or "",
                    "descendants": h.get("num_comments"),
                    "story_id": h.get("story_id"),
                }
            if len(hits) < 1000:
                break
            # Next page: strictly older than this page's oldest second (may drop a few same-second items).
            hi = min(h["created_at_i"] for h in hits)
            print(f"  {label} {tag}: {len(rows):,} rows, at {datetime.fromtimestamp(hi, timezone.utc):%m-%d %H:%M}", flush=True)
            time.sleep(0.2)
    table = pa.Table.from_pylist(list(rows.values()), schema=SCHEMA)
    pq.write_table(table, dest, compression="zstd")
    return True


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": "hackerproducts-prototype"}) as client:
        for y, m in months():
            d = RAW / f"month={y}-{m:02d}"
            dest = d / "items.parquet"
            if dest.exists():
                topup(client, y, m, d)
                print(f"{y}-{m:02d}: done")
                continue
            d.mkdir(parents=True, exist_ok=True)
            part = d / "items.parquet.part"
            ok = hf_month(client, y, m, part)
            if not ok:
                print(f"{y}-{m:02d}: not in dump, using Algolia", flush=True)
                algolia_month(client, y, m, part)
            part.rename(dest)
            n = duckdb.sql(f"SELECT count(*) FROM read_parquet('{dest}')").fetchall()[0][0]
            print(f"{y}-{m:02d}: {n:,} items", flush=True)
            topup(client, y, m, d)
    return 0


def topup(client, y, m, d):
    """The dump's last month can be partial: fill the rest of that month from Algolia."""
    dest = d / "topup.parquet"
    if dest.exists():
        return
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = int(datetime(ny, nm, 1, tzinfo=timezone.utc).timestamp())
    last = duckdb.sql(f"SELECT epoch(max(time))::BIGINT FROM read_parquet('{d / 'items.parquet'}')").fetchall()[0][0]
    if end - last < 6 * 3600:
        return
    print(f"{y}-{m:02d}: dump ends early, topping up from Algolia", flush=True)
    part = d / "topup.parquet.part"
    algolia_range(client, last + 1, end, part, f"{y}-{m:02d} topup")
    part.rename(dest)


if __name__ == "__main__":
    sys.exit(main())
