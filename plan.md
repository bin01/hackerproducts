# HackerProducts prototype: plan

Spec: the design doc at https://claude.ai/artifact/PWiJzwRfXzbKcFwQSQMj6i plus the brief from the project
thread (software only, one year of comments, no Person view). This file tracks what was built.

## Stages (Makefile targets)

| Stage | Script | Output | Notes |
|---|---|---|---|
| ingest | `pipeline/ingest.py` | `data/raw/month=YYYY-MM/items.parquet` | HF open-index dump through 23 Aug 2026, topped up from Algolia for the rest of Aug, Sep and Oct. Oct 2025 included only so parent chains resolve. |
| prefilter | `pipeline/prefilter.py` | `data/candidates.parquet` | DuckDB + regex: phrasing, non-news outbound links, Ask HN tool threads. Priority orders extraction. |
| gold | `pipeline/extract.py --ids data/gold/ids.txt` x3, `pipeline/gold_eval.py` | `data/gold/report.md`, `choice.txt` | 500 comments (300 priority>=3, 200 below), Haiku low, Sonnet low, Opus medium as reference. |
| extract | `pipeline/extract.py --model <choice> --top N` | `data/extract/<prompt>/<model>-<effort>/*.jsonl` | `claude -p` on the subscription, batches of 100, 4 workers, resumable by comment id, stops cleanly on usage limit. |
| hardcases | `pipeline/extract.py --model sonnet --ids data/hardcases.txt` | same layout | Comments where any product has confidence < 0.5. |
| resolve | `pipeline/resolve.py` | `data/hp.sqlite` (product, alias, mention, category, alias_review, hn_item) | Domain / GitHub repo first, then normalized names; Sonnet decides near-duplicate merges (cached). |
| score | `pipeline/score.py` | `product_score`, `comparison` | Distinct recommenders, one vote per user per product, recency half-life 3 years, warnings subtract, affiliated excluded. Rising = last 90 days vs trailing year. |
| synth | `pipeline/synth.py` | `theme`, `verdict` | Opus medium per product above a recommender threshold; validator rejects uncited claims. |
| publish | `pipeline/publish.py` | `site/src/data/*.json`, `site/public/export/*.json` | |
| site | `site/` (Astro) + Pagefind | `site/dist` | Home, Category, Product pages, search, JSON export, j/k, /, ?, dark mode. |

## Known gaps in v1
- No account-age filter (the dump has no user creation dates).
- Gold reference is Opus, not human labels.
- Only the 25,000 highest-priority of 347,262 candidates are labeled; `make extract TOP=50000` extends it.
- Verdicts for the top 160 products; `make synth` with `--limit` raised covers the other 129 with 5+ recommenders.
- 8 names wait in alias_review; no karma band (not in the dump).

## Results (1 Nov 2026)
- Gold set (500 comments, Opus reference): Haiku P 73.9% R 84.7% F1 78.9%; Sonnet P 89.9% R 85.8% F1 87.8%. Sonnet chosen.
- 3,768,623 comments -> 347,262 candidates -> 25,000 labeled (Sonnet low) + 798 hard cases (Sonnet medium).
- 22,431 mentions, 9,574 products, 574 with 3+ recommenders, 1,584 product pages, 160 Opus verdicts, 1,964 citations all resolving.

## Todo
- [x] Project scaffold (git, uv)
- [x] Ingest
- [x] Prefilter
- [x] Gold set labeling + report
- [x] Bulk extraction
- [x] Hard-case re-run
- [x] Resolve
- [x] Score
- [x] Synthesis
- [x] Publish + Astro site + Pagefind
- [x] Run locally, report URL
