"""Compare extractors on the gold set. Reference = Opus labels (no human labels yet).

Reports product-level precision/recall/F1 per model against the reference, stance agreement on
matched products, precision of "recommend" pairs, Haiku-vs-Sonnet agreement, verbatim quote rate.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "data" / "gold"
RUNS = {"haiku": "haiku-low", "sonnet": "sonnet-low", "opus": "opus-medium"}


def key(name):
    k = re.sub(r"[^a-z0-9]", "", name.lower())
    return re.sub(r"(app|js|io|hq)$", "", k) or k


def load(run, ids):
    out = {}
    for f in (ROOT / "data" / "extract" / "extract_v1" / run).glob("*.jsonl"):
        for line in f.open():
            r = json.loads(line)
            if r["id"] in ids:
                out[r["id"]] = r
    return out


def same(a, b):
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def compare(pred, ref, ids):
    tp = fp = fn = 0
    stance_ok = stance_n = 0
    rec_tp = rec_n = 0
    for i in ids:
        p = {key(x["name"]): x for x in pred.get(i, {}).get("products", [])}
        r = {key(x["name"]): x for x in ref.get(i, {}).get("products", [])}
        matched = set()
        for pk, px in p.items():
            rk = next((k for k in r if k not in matched and same(pk, k)), None)
            if rk is None:
                fp += 1
                if px.get("stance") == "recommend":
                    rec_n += 1
                continue
            matched.add(rk)
            tp += 1
            stance_n += 1
            stance_ok += px.get("stance") == r[rk].get("stance")
            if px.get("stance") == "recommend":
                rec_n += 1
                rec_tp += r[rk].get("stance") == "recommend"
        fn += len(r) - len(matched)
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0
    return {"precision": prec, "recall": rec, "f1": f1, "stance_agree": stance_ok / stance_n if stance_n else 0,
            "recommend_precision": rec_tp / rec_n if rec_n else 0, "n_pred": tp + fp}


def main():
    ids = {int(x) for x in (GOLD / "ids.txt").read_text().split()}
    data = {m: load(run, ids) for m, run in RUNS.items()}
    common = ids & set(data["opus"]) & set(data["haiku"]) & set(data["sonnet"])
    common = {i for i in common if not any(data[m][i]["missing"] for m in data)}
    lines = [f"# Gold set: {len(common)} comments labeled by all three models (reference = Opus, medium)", ""]
    lines.append("| Model | Products found | Precision | Recall | F1 | Stance agreement | 'Recommend' precision | Verbatim quotes |")
    lines.append("|---|---|---|---|---|---|---|---|")
    res = {}
    for m in ("haiku", "sonnet"):
        c = compare(data[m], data["opus"], common)
        prods = [p for i in common for p in data[m][i]["products"]]
        verb = sum(p.get("quote_verbatim", False) for p in prods) / len(prods) if prods else 0
        res[m] = c
        lines.append(f"| {m} | {c['n_pred']} | {c['precision']:.1%} | {c['recall']:.1%} | {c['f1']:.1%} | "
                     f"{c['stance_agree']:.1%} | {c['recommend_precision']:.1%} | {verb:.1%} |")
    n_ref = sum(len(data["opus"][i]["products"]) for i in common)
    hs = compare(data["haiku"], data["sonnet"], common)
    lines += ["", f"Opus found {n_ref} products. Haiku vs Sonnet (Sonnet as reference): F1 {hs['f1']:.1%}, "
              f"stance agreement {hs['stance_agree']:.1%}."]
    gap = res["sonnet"]["f1"] - res["haiku"]["f1"]
    gap_s = res["sonnet"]["stance_agree"] - res["haiku"]["stance_agree"]
    choice = "haiku" if gap <= 0.05 and gap_s <= 0.05 else "sonnet"
    lines += ["", f"F1 gap Sonnet minus Haiku: {gap * 100:+.1f} points; stance gap {gap_s * 100:+.1f} points. "
              f"Rule (within 5 points on both) picks: **{choice}** for the first pass."]
    report = "\n".join(lines)
    (GOLD / "report.md").write_text(report + "\n")
    (GOLD / "choice.txt").write_text(choice + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
