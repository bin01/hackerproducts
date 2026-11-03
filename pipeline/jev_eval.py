"""Compare Jev's scores (data/jev/scores.jsonl) with the local yield classifier on the same comments.

The classifier is retrained without the Jev-scored comments, so both are judged out of sample.
Ground truth: Sonnet found at least one product (and, separately, one in the ranked set).
"""
import json
import sqlite3
import sys
from pathlib import Path

import duckdb
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
from yield_model import CANDS, features, labels  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def top_k(y, s, frac):
    k = int(len(s) * frac)
    sel = y[np.argsort(-s)[:k]]
    return sel.mean(), sel.sum() / max(1, y.sum())


def main():
    jev = {}
    for line in (ROOT / "data" / "jev" / "scores.jsonl").open():
        r = json.loads(line)
        jev[r["id"]] = r
    lab = labels()
    db = sqlite3.connect(ROOT / "data" / "hp.sqlite")
    ranked_items = {i for (i,) in db.execute("""SELECT DISTINCT m.item_id FROM mention m JOIN product_score s
                                               ON s.product_id = m.product_id WHERE s.distinct_recommenders >= 3""")}
    rows = duckdb.sql(f"""SELECT id, text, coalesce(story_title, ''), priority, ask_tools, phrase, len(domains) > 0,
                                 repo_link, length(text) FROM read_parquet('{CANDS}')""").fetchall()
    train = [r for r in rows if r[0] in lab and r[0] not in jev]
    test = [r for r in rows if r[0] in jev and r[0] in lab]
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=3, max_features=300000, sublinear_tf=True)
    clf = LogisticRegression(max_iter=2000, C=4).fit(features(train, vec, fit=True), [lab[r[0]] for r in train])
    local = clf.predict_proba(features(test, vec))[:, 1]
    j = np.array([jev[r[0]]["p"] for r in test])
    rank_avg = (np.argsort(np.argsort(local)) + np.argsort(np.argsort(j))) / (2 * len(j))
    truths = {"has product": np.array([lab[r[0]] for r in test]),
              "has ranked product": np.array([int(r[0] in ranked_items) for r in test])}
    n_in = sum(jev[r[0]]["usage"].get("input_tokens", 0) for r in test)
    print(f"{len(test):,} comments; Jev input tokens {n_in:,} (${n_in / 1e6 * 0.042:.3f}); "
          f"median latency {int(np.median([jev[r[0]]['ms'] for r in test]))} ms")
    for tname, y in truths.items():
        print(f"\nTruth: {tname} (base rate {y.mean():.0%})")
        print("| Scorer | AUC | Top 30%: yield | Top 30%: recall | Top 50%: yield | Top 50%: recall |")
        print("|---|---|---|---|---|---|")
        for name, s in (("Local classifier", local), ("Jev", j), ("Average (rank)", rank_avg)):
            y30, r30 = top_k(y, s, 0.3)
            y50, r50 = top_k(y, s, 0.5)
            print(f"| {name} | {roc_auc_score(y, s):.3f} | {y30:.0%} | {r30:.0%} | {y50:.0%} | {r50:.0%} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
