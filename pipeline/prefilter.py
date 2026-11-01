"""Stage 2: cut the year of comments to recommendation candidates, no LLM.

Signals: recommendation phrasing, outbound links to non-news domains, Ask HN tool threads.
Each candidate carries its story title and parent comment as context, plus a priority
used to order extraction. Output: data/candidates.parquet (rebuilt from raw each run).
"""
import html
import re
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "candidates.parquet"
WINDOW_START = "2025-11-01"

NEWS = [
    "nytimes.com", "wsj.com", "bloomberg.com", "reuters.com", "theguardian.com", "bbc.co.uk", "bbc.com",
    "washingtonpost.com", "arstechnica.com", "theverge.com", "wikipedia.org", "ycombinator.com",
    "twitter.com", "x.com", "archive.org", "archive.ph", "archive.is", "reddit.com", "cnbc.com", "ft.com",
    "economist.com", "apnews.com", "cnn.com", "theatlantic.com", "wired.com", "techcrunch.com",
    "youtube.com", "youtu.be", "arxiv.org", "doi.org", "nature.com", "science.org", "forbes.com",
    "businessinsider.com", "axios.com", "politico.com", "npr.org", "latimes.com", "nypost.com",
    "bsky.app", "mastodon.social", "imgur.com", "google.com", "medium.com", "substack.com",
]

PHRASES = (
    r"\b(?:i|we)\s+(?:(?:have|'ve)\s+been\s+using|use|used|switched\s+to|moved\s+to|migrated\s+to|"
    r"recommend|love|swear\s+by|rely\s+on|pay\s+for|replaced\s+\w+(?:\s+\w+)?\s+with)\b"
    r"|highly\s+recommend|can'?t\s+recommend|cannot\s+recommend|strongly\s+recommend"
    r"|switched\s+(?:from|to)|moved\s+(?:away\s+)?from|moved\s+off|my\s+go-to|daily\s+driver"
    r"|been\s+using\s+\S+.{0,30}\s+for\s+(?:years|months|a\s+while)|alternative\s+to"
    r"|worth\s+every\s+penny|give\s+\S+\s+a\s+(?:try|shot)|check\s+out|take\s+a\s+look\s+at|stay\s+away\s+from"
)
ASK_TOOLS = (
    r"^(?:ask hn|tell hn)\b.*\b(?:recommend|recommendation|best|favou?rite|alternatives?|tools?|apps?|software|stack|setup|"
    r"(?:do|did|does|should) (?:you|i|we) use|you use|using for|use for|switch(?:ed)? to)\b"
)

ASK_EXCLUDE = (
    r"working on|building|hiring|wants to be hired|freelancer|personal (?:website|site|blog)|predictions"
    r"|did you read|made for yourself|you(?: have)? (?:made|built)|side project"
)

TAG_RE = re.compile(r"<[^>]+>")


def clean(s):
    if not s:
        return ""
    s = s.replace("<p>", "\n\n")
    s = re.sub(r'<a href="([^"]+)"[^>]*>[^<]*</a>', lambda m: html.unescape(m.group(1)), s)
    return html.unescape(TAG_RE.sub("", s)).strip()


def main():
    con = duckdb.connect()
    con.sql("SET preserve_insertion_order = false")
    con.sql(f"""
        CREATE TABLE items AS
        SELECT id, type, "by", time, text, parent, url, score, title, story_id
        FROM read_parquet('{RAW}/month=*/*.parquet', hive_partitioning = false, union_by_name = true)
        QUALIFY row_number() OVER (PARTITION BY id ORDER BY story_id NULLS LAST) = 1
    """)
    # Resolve each comment's root story by walking parents until the parent is a story or unknown.
    con.sql("""
        CREATE TABLE roots AS
        SELECT id, coalesce(story_id, parent) AS root FROM items WHERE type = 2
    """)
    for _ in range(80):
        con.sql("""
            UPDATE roots SET root = p.parent
            FROM items p WHERE p.id = roots.root AND p.type = 2
        """)
        changed = con.sql("SELECT count(*) FROM roots r JOIN items p ON p.id = r.root AND p.type = 2").fetchall()[0][0]
        if changed == 0:
            break
    news = ",".join(f"'{d}'" for d in NEWS)
    con.sql(f"""
        CREATE TABLE cand AS
        WITH c AS (
          SELECT i.id, i."by", i.time, i.text, i.parent, r.root AS story_id,
                 replace(i.text, '&#x2F;', '/') AS t
          FROM items i JOIN roots r USING (id)
          WHERE i.type = 2 AND i.time >= '{WINDOW_START}' AND i.text IS NOT NULL AND i."by" IS NOT NULL
            AND length(i.text) BETWEEN 40 AND 6000
        ), f AS (
          SELECT c.*,
            list_filter(
              list_distinct(list_transform(
                regexp_extract_all(t, 'href="https?://(?:www\\.)?([^/"]+)', 1), lambda d: lower(d))),
              lambda d: NOT list_contains([{news}], d) AND NOT list_bool_or(list_transform([{news}], lambda n: ends_with(d, '.' || n)))
            ) AS domains,
            regexp_matches(lower(t), '{PHRASES.replace("'", "''")}') AS phrase
          FROM c
        )
        SELECT f.id, f."by", f.time, f.text, f.parent, f.story_id, f.domains, f.phrase,
               s.title AS story_title, s.url AS story_url,
               (regexp_matches(lower(coalesce(s.title, '')), '{ASK_TOOLS}')
                AND NOT regexp_matches(lower(coalesce(s.title, '')), '{ASK_EXCLUDE}')) AS ask_tools,
               list_has_any(f.domains, ['github.com', 'gitlab.com', 'codeberg.org', 'sr.ht']) AS repo_link,
               CASE WHEN p.type = 2 THEN p.text END AS parent_text,
               p.type = 1 AS top_level
        FROM f
        LEFT JOIN items s ON s.id = f.story_id AND s.type = 1
        LEFT JOIN items p ON p.id = f.parent
        WHERE f.phrase OR len(f.domains) > 0 OR (regexp_matches(lower(coalesce(s.title, '')), '{ASK_TOOLS}')
              AND NOT regexp_matches(lower(coalesce(s.title, '')), '{ASK_EXCLUDE}'))
    """)
    rows = con.sql("""
        SELECT *, (CASE WHEN ask_tools THEN 3 ELSE 0 END) + (CASE WHEN phrase THEN 2 ELSE 0 END)
                + (CASE WHEN len(domains) > 0 THEN 1 ELSE 0 END) + (CASE WHEN repo_link THEN 0.5 ELSE 0 END)
                + (CASE WHEN top_level THEN 0.5 ELSE 0 END)
                + (CASE WHEN length(text) BETWEEN 120 AND 2500 THEN 0.5 ELSE 0 END) AS priority
        FROM cand
    """).fetchall()
    cols = [d[0] for d in con.sql("SELECT *, 0.0 AS priority FROM cand LIMIT 0").description]
    import pyarrow as pa
    import pyarrow.parquet as pq
    data = {c: [r[i] for r in rows] for i, c in enumerate(cols)}
    data["text"] = [clean(t) for t in data["text"]]
    data["parent_text"] = [clean(t)[:800] if t else None for t in data["parent_text"]]
    pq.write_table(pa.table(data), OUT, compression="zstd")

    total = con.sql(f"SELECT count(*) FROM items WHERE type = 2 AND time >= '{WINDOW_START}'").fetchall()[0][0]
    stats = duckdb.sql(f"""
        SELECT count(*), count(*) FILTER (WHERE ask_tools), count(*) FILTER (WHERE phrase),
               count(*) FILTER (WHERE len(domains) > 0), count(*) FILTER (WHERE priority >= 3)
        FROM read_parquet('{OUT}')
    """).fetchall()[0]
    print(f"comments in window: {total:,}")
    print(f"candidates: {stats[0]:,} ({stats[0] / total:.1%}); ask-tools threads {stats[1]:,}, "
          f"phrasing {stats[2]:,}, non-news link {stats[3]:,}, priority>=3 {stats[4]:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
