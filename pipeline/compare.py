"""Numbers for comparing AI model families: releases and their time windows, use buckets, pairs and switches.

model_stats.py exports these as aggregates. publish.py uses them for /models/compare/ with linked example comments.
"""
import re
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from publish import MODELS, MONTHS, family_parents, version_of  # noqa: E402
from resolve import name_key  # noqa: E402

MIN_RELEASE = 5
STANCES = ("recommend", "neutral", "compare", "warn")

# A mention can fall in several buckets ("local coding agent"). The category label only counts when it names a specific job.
BUCKETS = {
    "coding": ("Coding", r"\bcod(e|es|ing|er)\b|program|debug|refactor|software|\bdevelop(ment|er|ing)?\b|implementation|reverse engineering|frontend|backend|\bsql\b|compiler|\bscripts?\b", r"coding"),
    "agents": ("Agents", r"\bagents?\b|\bagentic\b|subagent|autonomous|tool (use|calling)|\bmcp\b", r"\bagents?\b"),
    "local": ("Running locally", r"\blocal(ly)?\b|self-host|on-device|offline|homelab|home server|ollama|llama\.cpp|quantiz", r"\blocal\b"),
    "writing": ("Writing", r"\bprose\b|essay|\bblog|copywrit|\bdraft|\bemails?\b|fiction|translat|proofread|rewriting text|\bwriting\b", None),
    "research": ("Research and search", r"research|\bsearch|summar|\blearn|\bstudy|explain|fact.?check|\bpapers?\b(?!-)|knowledge|data analysis", None),
    "vision": ("Images and video", r"\bimages?\b|\bvision\b|\bphotos?\b|\bvideos?\b|\bocr\b|screenshot|\bsvg\b|diffusion", r"\b(image|video|vision)\b"),
    "speech": ("Speech and audio", r"speech|\baudio\b|\bvoice\b|transcri|\btts\b|\bstt\b|\basr\b|dictation", r"speech|\baudio\b|\bvoice\b|transcri"),
    "chat": ("Everyday chat", r"\bchat(s|bots?)?\b|\bassistant\b|general (use|ai|llm|purpose|questions?)|everyday|\bdaily (use|ai|questions?)|personal use|conversation|advice|companion", None),
}
NOT_PROSE = re.compile(r"cod(e|ing)|\btests?\b|compiler|software|\bapps?\b|project|script|kernel|parser|program|\bai\b|skill|tool|\bsql\b", re.I)
PROSE = re.compile(r"\band writing\b|\bwriting (help|assistance|documentation)|creative", re.I)
_rx = {k: (re.compile(u, re.I), re.compile(c, re.I) if c else None) for k, (_, u, c) in BUCKETS.items()}


def buckets_of(m):
    use, cat = m["use_case"] or "", m["category_raw"] or ""
    out = {k for k, (u, c) in _rx.items() if u.search(use) or (c and c.search(cat))}
    if "coding" in out and "chat" in out and not re.search(r"\bchat|general|everyday", use, re.I):
        out.discard("chat")  # "AI coding assistant" is about coding
    # "writing a compiler" and "writing unit tests" are coding, not writing.
    if "writing" in out and re.search(r"\bwriting\b", use, re.I) and NOT_PROSE.search(use) and not PROSE.search(use):
        out.discard("writing")
    return out


def people_stance(ms):
    latest = {}
    for m in sorted(ms, key=lambda m: m["time"]):
        latest[m["by"]] = m["stance"]
    c = Counter(latest.values())
    return {"people": len(latest), **{s: c[s] for s in STANCES}}


def monthly(ms):
    per = defaultdict(dict)
    for m in sorted(ms, key=lambda m: m["time"]):
        per[m["time"][:7]][m["by"]] = m["stance"]
    return {mo: {s: Counter(per[mo].values())[s] for s in STANCES} for mo in MONTHS if mo in per}


def net(c):
    return round(100 * (c["recommend"] - c["warn"]) / c["people"]) if c["people"] else None


def release_label(name, v):
    if re.search(rf"(?<![\d.]){re.escape(v)}(?![\d.])", name):
        return name
    head = re.match(r"(.*?)(\d+)$", name)
    return f"{head[1]}{v}" if head and v.startswith(head[2]) else f"{name} {v}"


def quarter(d):
    y, m = int(d[:4]), int(d[5:7])
    return f"Q{(m - 1) // 3 + 1} {y}"


def line_releases(line_ms):
    """Releases of one model line with their windows: from a release's 3rd mention until the next release's."""
    by_v = defaultdict(list)
    for m in line_ms:
        v = version_of(m["raw_name"])
        if v:
            by_v[v].append(m)
    keep = {v: sorted(x, key=lambda m: m["time"]) for v, x in by_v.items() if len({m["by"] for m in x}) >= MIN_RELEASE}
    third = {v: x[min(2, len(x) - 1)]["time"][:10] for v, x in keep.items() if not v.endswith("B")}
    out = []
    for v, x in keep.items():
        row = {"version": v, "first_seen": x[0]["time"][:10], "third_mention_date": third.get(v), "window_start": None, "window_end": None,
               "all_time": people_stance(x), "in_window": None, "current": False}
        if v in third:
            later = [third[o] for o in third if float(o) > float(v)]
            end = min(later) if later else None
            if end is None or end > third[v]:  # otherwise a newer release was already out: an old release talked about in hindsight
                inside = [m for m in x if third[v] <= m["time"][:10] and (end is None or m["time"][:10] < end)]
                row.update(window_start=third[v], window_end=end, in_window=people_stance(inside), current=end is None)
        out.append(row)
    out.sort(key=lambda r: -float(r["version"].rstrip("B")))
    return out


class Ctx:
    """Mentions grouped by model family, without people who built that family."""
    def __init__(self, products, cats, mentions, alias):
        self.products, self.mentions = products, mentions
        self.parent_of = family_parents(products, cats)
        self.model_ids = {pid for pid, p in products.items() if cats[p["category_id"]]["slug"] == MODELS}
        self.family_of = {pid: self.parent_of.get(pid, pid) for pid in self.model_ids}
        self.by_key = alias
        grouped = defaultdict(list)
        for pid in self.model_ids:
            grouped[self.family_of[pid]].extend(mentions[pid])
        self.fam_ms = {}
        for f, ms in grouped.items():
            makers = {m["by"] for m in ms if m["affiliated"]}
            self.fam_ms[f] = [m for m in ms if m["by"] not in makers]
        self.top = sorted(self.fam_ms, key=lambda f: -len({m["by"] for m in self.fam_ms[f] if m["stance"] == "recommend"}))

    def slug(self, f):
        return self.products[f]["slug"]

    def name(self, f):
        return self.products[f]["name"]

    def family_key(self, raw):
        pid = self.by_key.get(name_key(str(raw or "")))
        return self.family_of.get(pid)

    def releases(self):
        out = []
        for pid in self.model_ids:
            ms = [m for m in self.mentions[pid] if not m["affiliated"]]
            for r in line_releases(ms):
                f = self.family_of[pid]
                out.append({"family": self.slug(f), "family_name": self.name(f), "line": self.products[pid]["name"], "slug": self.products[pid]["slug"],
                            "label": release_label(self.products[pid]["name"], r["version"]), **r})
        return out

    def pairs(self, among):
        """Comments naming two families (or listing one as the alternative), and which one the comment favoured."""
        among = set(among)
        items = defaultdict(lambda: defaultdict(list))
        alts = defaultdict(set)
        for f in among:
            for m in self.fam_ms[f]:
                items[m["item_id"]][f].append(m)
                for a in _alts(m):
                    g = self.family_key(a)
                    if g in among and g != f:
                        alts[m["item_id"]].add(g)
        out = defaultdict(lambda: {"comments": 0, "a": 0, "b": 0, "items": []})
        for item, fams in items.items():
            present = set(fams) | alts[item]
            for a, b in combinations(sorted(present, key=lambda f: self.top.index(f)), 2):
                r = out[(a, b)]
                r["comments"] += 1
                pref = _prefers(a, b, fams, self)
                if pref == a:
                    r["a"] += 1
                elif pref == b:
                    r["b"] += 1
                if a in fams and b in fams:
                    r["items"].append((item, pref))
        return out

    def switches(self):
        flows = defaultdict(set)
        for f, ms in self.fam_ms.items():
            for m in ms:
                g = self.family_key(m["switched_from"]) if m["switched_from"] else None
                if g and g != f:
                    flows[(g, f)].add(m["by"])
        return flows


def _alts(m):
    import json
    try:
        return json.loads(m["alternatives"] or "[]")
    except ValueError:
        return []


def _prefers(a, b, fams, ctx):
    def stance(f):
        st = {m["stance"] for m in fams.get(f, [])}
        return "mixed" if {"recommend", "warn"} <= st else "recommend" if "recommend" in st else "warn" if "warn" in st else "compare" if "compare" in st else "neutral"
    sa, sb = stance(a), stance(b)
    for f, other, s_f, s_o in ((a, b, sa, sb), (b, a, sb, sa)):
        moved = any(ctx.family_key(m["switched_from"]) == other for m in fams.get(f, []) if m["switched_from"])
        if s_f == "recommend" and (s_o in ("warn", "compare") and other in fams or moved):
            return f
    return None


def _products_by_slug(self, slug):
    return next(pid for pid, p in self.products.items() if p["slug"] == slug)


Ctx.products_by_slug = _products_by_slug


OPEN_FAMILIES = {"qwen", "kimi", "glm", "deepseek", "gemma", "mimo", "mistral", "gpt-oss", "llama", "olmo", "ibm-granite", "nemotron", "minimax"}
FAST_LINE = re.compile(r"haiku|flash|luna|composer", re.I)
MIN_LOVE_PEOPLE = 20
LOVE_SKIP = {"nvidia-parakeet", "GPT-4", "Gemini 3"}  # a speech model, and rows that mix several model lines


def love(c):
    d = c["recommend"] + c["warn"]
    return round(100 * c["recommend"] / d) if d else None


def love_releases(releases):
    """Releases with enough people during their window for a love score: recommend / (recommend + warn)."""
    out = []
    for r in releases:
        w = r["in_window"]
        if not w or w["people"] < MIN_LOVE_PEOPLE or not (w["recommend"] + w["warn"]) or r["label"] in LOVE_SKIP or r["slug"] in LOVE_SKIP:
            continue
        out.append({"l": r["label"], "f": r["family_name"], "ln": r["line"], "slug": r["slug"], "v": r["version"], "s": r["window_start"], "e": r["window_end"],
                    "cur": r["current"], "p": w["people"], "r": w["recommend"], "w": w["warn"], "n": w["neutral"], "c": w["compare"],
                    "o": r["family"] in OPEN_FAMILIES, "t": "fast" if FAST_LINE.search(r["line"]) else "flag"})
    return sorted(out, key=lambda x: x["s"])


def quote(m, ctx, f):
    return {"name": ctx.name(f), "by": m["by"], "date": m["time"][:10], "url": f"https://news.ycombinator.com/item?id={m['item_id']}",
            "stance": m["stance"], "quote": m["reason_quote"]}


def verbatim(m):
    return m["quote_verbatim"] and m["reason_quote"]


def page_data(ctx, summaries):
    """Everything /models/compare/ shows, plus the example comments it loads when a cell is opened."""
    by_slug = {s["slug"]: s for s in summaries}
    rows = [by_slug[ctx.slug(f)] for f in ctx.top if ctx.slug(f) in by_slug and by_slug[ctx.slug(f)]["listed"] and not by_slug[ctx.slug(f)].get("family")]
    releases = ctx.releases()
    quotes = {}

    def newest(family):
        now = [r for r in releases if r["family"] == family and r["current"] and r["in_window"]["people"] >= 10]
        return max(now, key=lambda r: (float(r["version"]), r["all_time"]["people"])) if now else None

    table = []
    fam_by_slug = {ctx.slug(f): f for f in ctx.fam_ms}
    for s in rows[:20]:
        c = people_stance(ctx.fam_ms[fam_by_slug[s["slug"]]])
        row = {"slug": s["slug"], "name": s["name"], "icon": s["icon"], "recommenders": s["recommenders"], "people": c["people"],
               "rec": c["recommend"], "warn": c["warn"], "other": c["neutral"] + c["compare"], "praise": s["loved"], "caveat": s["caveat"], "latest": None}
        r = newest(s["slug"])
        if r:
            before = [o for o in releases if o["slug"] == r["slug"] and o["in_window"] and o["in_window"]["people"] >= MIN_RELEASE and o["window_end"] == r["window_start"]]
            row["latest"] = {"label": r["label"], "slug": r["slug"], "v": r["version"], "love": love(r["in_window"]), "people": r["in_window"]["people"],
                             "prev": before[0]["label"] if before else None, "delta": love(r["in_window"]) - love(before[0]["in_window"]) if before and love(r["in_window"]) is not None and love(before[0]["in_window"]) is not None else None}
        table.append(row)

    families = [fam_by_slug[r["slug"]] for r in table[:14]]
    matrix = []
    for f in families:
        cells = {}
        for b in BUCKETS:
            ms = [m for m in ctx.fam_ms[f] if b in buckets_of(m)]
            c = people_stance(ms)
            if c["people"] >= MIN_RELEASE:
                cells[b] = {"people": c["people"], "pct": round(100 * c["recommend"] / c["people"])}
                ordered = sorted((m for m in ms if verbatim(m)), key=lambda m: (m["stance"] != "recommend", m["stance"] != "warn", m["time"]), reverse=False)
                quotes[f"cell:{ctx.slug(f)}:{b}"] = [quote(m, ctx, f) for m in ordered[:6]]
        matrix.append({"slug": ctx.slug(f), "name": ctx.name(f), "icon": by_slug[ctx.slug(f)]["icon"], "cells": cells})

    listed = [fam_by_slug[r["slug"]] for r in rows]
    pairs = ctx.pairs(listed)
    pair_rows = []
    for (a, b), v in sorted(pairs.items(), key=lambda kv: -kv[1]["comments"])[:12]:
        key = f"pair:{ctx.slug(a)}:{ctx.slug(b)}"
        pair_rows.append({"key": key, "a": ctx.name(a), "b": ctx.name(b), "a_slug": ctx.slug(a), "b_slug": ctx.slug(b),
                          "comments": v["comments"], "a_pref": v["a"], "b_pref": v["b"]})
        picks = sorted(v["items"], key=lambda x: (x[1] is None, -x[0]))[:12]
        out, seen = [], 0
        for item, _ in picks:
            got = [quote(m, ctx, g) for g in (a, b) for m in ctx.fam_ms[g] if m["item_id"] == item and verbatim(m)][:2]
            if len(got) == 2:
                out.append(got)
            if len(out) == 5:
                break
        quotes[key] = out
    top8 = families[:8]
    grid = {"families": [{"slug": ctx.slug(f), "name": ctx.name(f)} for f in top8],
            "counts": {f"{ctx.slug(a)}:{ctx.slug(b)}": pairs[(a, b)]["comments"] for a, b in pairs if a in top8 and b in top8}}
    for f, g in combinations(top8, 2):
        key = (f, g) if (f, g) in pairs else (g, f)
        qkey = f"pair:{ctx.slug(key[0])}:{ctx.slug(key[1])}"
        if key in pairs and qkey not in quotes:
            v = pairs[key]
            out = []
            for item, _ in sorted(v["items"], key=lambda x: (x[1] is None, -x[0]))[:12]:
                got = [quote(m, ctx, h) for h in key for m in ctx.fam_ms[h] if m["item_id"] == item and verbatim(m)][:2]
                if len(got) == 2:
                    out.append(got)
                if len(out) == 5:
                    break
            quotes[qkey] = out

    flows = ctx.switches()
    flow_rows = []
    for (a, b), who in sorted(flows.items(), key=lambda kv: -len(kv[1]))[:20]:
        key = f"flow:{ctx.slug(a)}:{ctx.slug(b)}"
        flow_rows.append({"key": key, "from": ctx.name(a), "to": ctx.name(b), "from_slug": ctx.slug(a), "to_slug": ctx.slug(b), "people": len(who),
                          "back": len(flows.get((b, a), ()))})
        quotes[key] = [quote(m, ctx, b) for m in ctx.fam_ms[b] if m["switched_from"] and ctx.family_key(m["switched_from"]) == a and verbatim(m)][:6]
    loved = love_releases(releases)
    scores = sorted(100 * x["r"] / (x["r"] + x["w"]) for x in loved)
    return {"releases": loved, "typical": round(scores[len(scores) // 2]) if scores else 75, "table": table, "buckets": [{"key": k, "label": v[0]} for k, v in BUCKETS.items()], "matrix": matrix, "pairs": pair_rows, "grid": grid,
            "flows": flow_rows, "flow_total": sum(len(v) for v in flows.values())}, quotes
