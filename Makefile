# Each stage is resumable; LLM stages skip work already cached under data/extract and data/synth.
TOP ?= 25000
SYNTH_MIN ?= 5

.PHONY: all ingest prefilter gold extract hardcases resolve score synth publish site serve deploy

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
