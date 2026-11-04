# Each stage is resumable; LLM stages skip work already cached under data/extract and data/synth.
TOP ?= 25000
# Hidden from the site (games stay): social networks, streaming services, formats and protocols.
export EXCLUDE_KINDS ?= social,streaming,format
SYNTH_MIN ?= 5

.PHONY: all ingest prefilter gold extract yield extract-yield hardcases resolve score synth publish site serve deploy

all: ingest prefilter extract hardcases resolve score synth publish site

ingest:
	uv run pipeline/ingest.py

prefilter:
	uv run pipeline/prefilter.py

gold:
	uv run pipeline/extract.py --model haiku --effort low --ids data/gold/ids.txt --batch 50
	uv run pipeline/extract.py --model sonnet --effort low --ids data/gold/ids.txt --batch 50
	uv run pipeline/extract.py --model opus --effort medium --ids data/gold/ids.txt --batch 50
	uv run pipeline/gold_eval.py

extract:
	uv run pipeline/extract.py --model $$(cat data/gold/choice.txt) --effort low --top $(TOP) --workers 5

# Rank unlabeled candidates by predicted yield, then label only the best (cheaper per product found).
yield:
	uv run pipeline/yield_model.py

extract-yield:
	uv run pipeline/extract.py --model sonnet --effort low --prompt extract_v2 --scores data/unlabeled_scores.npy --min-score 0.5 --top $(TOP) --batch 100 --workers 10

hardcases:
	uv run pipeline/hardcases.py
	uv run pipeline/extract.py --model sonnet --effort medium --ids data/hardcases.txt --batch 50 --workers 5

resolve:
	uv run pipeline/resolve.py

score:
	uv run pipeline/score.py

synth:
	uv run pipeline/synth.py --min $(SYNTH_MIN) --limit 5000 --workers 8

publish:
	uv run pipeline/official.py
	uv run pipeline/publish.py
	uv run pipeline/icons.py
	uv run pipeline/publish.py

site:
	cd site && npm run build

serve:
	cd site && npm run preview

# Build and publish to Cloudflare Pages (project "hackerproducts", production branch main).
deploy: site
	cd site && npx --yes wrangler pages project list | grep -q '^│ hackerproducts ' || npx --yes wrangler pages project create hackerproducts --production-branch main
	cd site && npx --yes wrangler pages deploy dist --project-name hackerproducts --branch main --commit-dirty=true

# Family-wide themes for the top AI model families (about 11 Opus calls, cached), and aggregates for chart prototypes.
family-synth:
	uv run pipeline/family_synth.py

model-stats:
	uv run pipeline/model_stats.py data/transfer/models/model_stats.json
