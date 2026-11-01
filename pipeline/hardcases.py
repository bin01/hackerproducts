"""Pick first-pass comments with any product below the confidence cutoff for a Sonnet medium re-run."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CUTOFF = 0.5


def main():
    ids = set()
    for f in (ROOT / "data" / "extract" / "extract_v1" / "sonnet-low").glob("*.jsonl"):
        for line in f.open():
            r = json.loads(line)
            if r["missing"] or any(float(p.get("confidence") or 0) < CUTOFF for p in r["products"]):
                ids.add(r["id"])
    (ROOT / "data" / "hardcases.txt").write_text("\n".join(map(str, sorted(ids))) + "\n")
    print(f"hard cases: {len(ids):,} comments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
