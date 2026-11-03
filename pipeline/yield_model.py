"""Train a cheap local classifier that predicts whether a candidate comment names a product,
from the comments already labeled, and score the unlabeled candidates with it.

Labeling only high-scoring comments roughly doubles products found per LLM dollar
(held-out AUC 0.92; the top half of comments by score holds ~87% of productive ones).
Output: data/unlabeled_scores.npy, a 2 x N array of (comment id, probability).
"""
import glob
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parent.parent
CANDS = ROOT / "data" / "candidates.parquet"
OUT = ROOT / "data" / "unlabeled_scores.npy"


def labels():
    lab = {}
    for f in glob.glob(str(ROOT / "data" / "extract" / "extract_v*" / "sonnet-low" / "*.jsonl")):
        for line in open(f):
            r = json.loads(line)
            if not r["missing"]:
                lab[r["id"]] = int(len(r["products"]) > 0)
    return lab


def features(rows, vec, fit=False):
    text = [r[1][:2000] + " || " + r[2] for r in rows]
    x = vec.fit_transform(text) if fit else vec.transform(text)
    num = csr_matrix(np.array([[float(r[3]), r[4], r[5], r[6], r[7], np.log1p(r[8])] for r in rows], dtype=float))
    return hstack([x, num]).tocsr()


def main():
    lab = labels()
    rows = duckdb.sql(f"""SELECT id, text, coalesce(story_title, ''), priority, ask_tools, phrase, len(domains) > 0,
                                 repo_link, length(text) FROM read_parquet('{CANDS}')""").fetchall()
    known = [r for r in rows if r[0] in lab]
    unknown = [r for r in rows if r[0] not in lab]
    y = np.array([lab[r[0]] for r in known])
    tr, te = train_test_split(range(len(known)), test_size=0.2, random_state=0)
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=3, max_features=300000, sublinear_tf=True)
    clf = LogisticRegression(max_iter=2000, C=4).fit(features([known[i] for i in tr], vec, fit=True), y[tr])
    p = clf.predict_proba(features([known[i] for i in te], vec))[:, 1]
    print(f"labeled {len(known):,} (yield {y.mean():.0%}); held-out AUC {roc_auc_score(y[te], p):.3f}")
    scores = clf.predict_proba(features(unknown, vec))[:, 1]
    np.save(OUT, np.array([[r[0] for r in unknown], scores]))
    for t in (0.5, 0.7):
        m = scores >= t
        print(f"unlabeled with score >= {t}: {m.sum():,}, expected productive ~{scores[m].sum():,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
