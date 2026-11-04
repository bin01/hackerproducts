"""Stage 7: export data/hp.sqlite to JSON for the Astro site and the public JSON export.

site/src/data/catalog.json           lists, categories, stats
site/src/data/products/<slug>.json   one per product page
site/public/export/*.json            downloadable data (full catalog, per category)
"""
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hp.sqlite"
SITE = ROOT / "site"
DATA = SITE / "src" / "data"
EXPORT = SITE / "public" / "export"
MONTHS = [f"{y}-{m:02d}" for y, m in [(2025, 11), (2025, 12)] + [(2026, i) for i in range(1, 11)]]
MIN_PAGE = 2   # distinct recommenders for a product page (keeps the deploy under Cloudflare Pages' 20,000-file cap)
MODELS = "ai-models"
MIN_LIST = 3   # distinct recommenders to appear in ranked lists


# Repos whose name differs from the product but which are the product's real home (reviewed by hand).
REPO_OK = {"uBlock Origin Lite", "DwarfStar", "Droid-ify", "Projectivy", "Turbo Vision", "MicroQuickJS", "bwrap", "Qwen Coder",
           "Max", "ULID", "CoreCLR", "Synctrain", "GitHub Agentic Workflows", "Whispering", "icloudpd", "1-Bit Bonsai",
           "llama-server", "GE-Proton", "Amla Sandbox", "Visual Pinball", "Speech Note", "claude-trace", "Google Wave",
           "Jujutsu", "Microsoft Edit", "XLibre", "Windows Terminal", "Z-Image Turbo", "Dear ImGui", "Step Flash"}
# Known right answers where the lookup or a stray comment link got it wrong.
URL_FIXES = {"GPT Sol": "https://openai.com", "GPT Luna": "https://openai.com", "GPT Terra": "https://openai.com",
             "GPT Astra": "https://openai.com", "GPT-5 Pro": "https://openai.com", "Claude Fable": "https://claude.com",
             "Claude Cowork": "https://claude.com", "Claude Skills": "https://claude.com", "Jev": "https://typesafe.ai",
             "GitHub Copilot": "https://github.com/features/copilot", "GitHub Actions": "https://github.com/features/actions",
             "Muse Spark": "https://meta.ai", "Muse": "https://meta.ai", "Muse Glimmer": "https://meta.ai",
             "Mercury": "https://www.inceptionlabs.ai", "Voxtral": "https://mistral.ai", "Nemotron": "https://www.nvidia.com"}


def _key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _names_match(name, other):
    """The name appears in the other string, or the other covers most of the name ("sol" is not "GPT Sol")."""
    a, b = _key(name), _key(other)
    return bool(a and b) and (a in b or (b in a and len(b) >= 0.6 * len(a)))


def official_url(p, official):
    """The product's link, dropped rather than shown when a repo or a comment's domain doesn't match the product."""
    name = p["name"]
    if name in URL_FIXES:
        return URL_FIXES[name]
    url = p["official_url"] or official.get(name)
    if url:
        m = re.match(r"https?://(?:www\.)?(?:github\.com|gitlab\.com|codeberg\.org)/[^/]+/([^/#?]+)", url)
        return url if not m or name in REPO_OK or _names_match(name, m.group(1)) else ""
    if p["github_repo"]:
        repo = p["github_repo"].split("/", 1)[1]
        return f"https://github.com/{repo}" if name in REPO_OK or _names_match(name, repo.split("/")[-1]) else ""
    if p["domain"] and _names_match(name, p["domain"].split(".")[0]):
        return f"https://{p['domain']}"
    return ""


SIZE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[bBmM](?![a-zA-Z])")
MIN_VERSION_PEOPLE = 5  # enough people for a version's sentiment to mean something


def version_of(raw):
    """The release a mention names: "Claude Opus 4.8" -> "4.8", "Qwen 3.6 27B" -> "3.6", "gpt-oss-120b" -> "120B"."""
    s = re.sub(r"1[- ]bit", "", raw or "", flags=re.I)
    sizes = SIZE.findall(s)
    s = re.sub(r"\ba\d+b\b", "", SIZE.sub(" ", s), flags=re.I)
    for n in re.findall(r"(?<![\d.])\d+(?:\.\d+)?", s):
        if float(n) < 100:
            return n[:-2] if n.endswith(".0") else n
    return f"{sizes[0]}B" if sizes else None


def version_table(ms):
    """Per-version sentiment for one model line, latest release first. One person counts once per version."""
    rows = defaultdict(dict)
    unnamed = set()
    for m in sorted(ms, key=lambda m: m["time"]):
        v = version_of(m["raw_name"])
        if v is None:
            unnamed.add(m["by"])
        else:
            rows[v][m["by"]] = (m["stance"], m["time"][:10])  # their latest stance on this version
    out = []
    for v, people in rows.items():
        if len(people) >= MIN_VERSION_PEOPLE:
            st = Counter(x[0] for x in people.values())
            out.append({"v": v, "people": len(people), "rec": st["recommend"], "warn": st["warn"],
                        "other": st["neutral"] + st["compare"], "first": min(x[1] for x in people.values())})
    # Sizes ("27B") only count as versions for lines with no release numbers at all, such as gpt-oss.
    numbered = [r for r in out if not r["v"].endswith("B")]
    out = numbered or out
    # Latest release first. Order by version number, not first-seen, since the data only covers one year.
    out.sort(key=lambda r: -float(r["v"].rstrip("B")))
    return out, len(unnamed)


# One row per model family in the AI models list. Tiers keep their own pages, linked from the family page.
FAMILY_RULES = [
    ("claude", r"claude-"), ("gpt", r"gpt-(?!oss)|o[134](-|$)|chatgpt$"), ("gemini", r"gemini-"), ("deepseek", r"deepseek-"),
    ("glm", r"glm-|z-ai$"), ("mistral", r"mistral-|ministral|devstral|codestral|voxtral|magistral|le-chat$"),
    ("gemma", r"(diffusion|function|med|t|pali|shield)gemma|gemma-"), ("qwen", r"qwen-"), ("muse", r"muse-"), ("mimo", r"mimo-"),
    ("nemotron", r"nemotron-"), ("nano-banana", r"nano-banana-"), ("grok", r"grok-"), ("flux", r"flux-"),
    ("kimi", r"kimi-"), ("llama", r"llama-"), ("minimax", r"minimax-"), ("whisper", r"whisper-"), ("ornith", r"ornith-"),
]
ASSISTANT_OF = {}  # consumer assistants that keep their own row; none now that ChatGPT is a line of GPT, as Le Chat is of Mistral


def family_parents(products, cats):
    """{tier product id: family product id} for AI models."""
    by_slug = {p["slug"]: pid for pid, p in products.items() if cats[p["category_id"]]["slug"] == MODELS}
    out = {}
    for slug, pid in by_slug.items():
        for parent, rx in FAMILY_RULES:
            if slug != parent and parent in by_slug and re.match(rx, slug):
                out[pid] = by_slug[parent]
                break
    return out


def people_stance(ms):
    """Each person's latest stance, counted once."""
    latest = {}
    for m in sorted(ms, key=lambda m: m["time"]):
        latest[m["by"]] = m["stance"]
    st = Counter(latest.values())
    return {"people": len(latest), "rec": st["recommend"], "warn": st["warn"], "other": st["neutral"] + st["compare"]}


def family_rollup(ms):
    """A family's numbers: distinct people across its tiers, versions and plain mentions, same rules as score.py."""
    makers = {m["by"] for m in ms if m["affiliated"]}
    ms = sorted((m for m in ms if m["by"] not in makers), key=lambda m: m["time"])
    latest = {m["by"]: m["stance"] for m in ms}
    months, seen = defaultdict(Counter), set()
    for m in ms:
        k = (m["by"], m["time"][:7])
        if k not in seen:
            seen.add(k)
            months[m["time"][:7]][m["stance"]] += 1
    return {"recommenders": len({m["by"] for m in ms if m["stance"] == "recommend"}), "commenters": len(latest),
            "warners": sum(v == "warn" for v in latest.values()), "first": ms[0]["time"][:10], "latest": ms[-1]["time"][:10],
            "months": {k: dict(v) for k, v in months.items()}}


def latest_of(lines, mentions_of):
    """The newest current release across model lines, largest line first on ties, with its love score.
    Same numbers as the charts: only comments from while it was newest, and at least 10 people."""
    from compare import line_releases, love, release_label
    best, label = None, None
    for line in lines:
        for r in line_releases([m for m in mentions_of(line["slug"]) if not m["affiliated"]]):
            w = r["in_window"]
            rank = (float(r["version"]), r["all_time"]["people"]) if r["current"] else None
            if r["current"] and w["people"] >= 10 and love(w) is not None and (best is None or rank > (float(best["version"]), best["all_time"]["people"])):
                best, label = r, release_label(line["name"], r["version"])
    if not best:
        return None
    w = best["in_window"]
    return {"label": label, "love": love(w), "people": w["people"], "rec": w["recommend"], "warn": w["warn"], "other": w["neutral"] + w["compare"]}


def hn(item_id):
    return f"https://news.ycombinator.com/item?id={item_id}"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cats = {r["id"]: dict(r) for r in con.execute("SELECT * FROM category")}
    scores = {r["product_id"]: dict(r) for r in con.execute("SELECT * FROM product_score")}
    verdicts = {r["product_id"]: dict(r) for r in con.execute("SELECT * FROM verdict")}
    themes = defaultdict(list)
    for r in con.execute("SELECT * FROM theme ORDER BY id"):
        themes[r["product_id"]].append(dict(r))
    mentions = defaultdict(list)
    for r in con.execute("""SELECT m.*, i.by, i.time, i.story_id, i.story_title, i.text AS comment
                            FROM mention m JOIN hn_item i ON i.id = m.item_id ORDER BY i.time DESC"""):
        mentions[r["product_id"]].append(dict(r))
    products = {r["id"]: dict(r) for r in con.execute("SELECT * FROM product")}
    comps = defaultdict(list)
    for r in con.execute("SELECT * FROM comparison WHERE co_mentions + switched_a_to_b + switched_b_to_a >= 2"):
        comps[r["product_a"]].append((r["product_b"], r["co_mentions"], r["switched_b_to_a"], r["switched_a_to_b"]))
        comps[r["product_b"]].append((r["product_a"], r["co_mentions"], r["switched_a_to_b"], r["switched_b_to_a"]))

    off_f = ROOT / "data" / "resolve" / "official_v1.json"
    official = json.loads(off_f.read_text()) if off_f.exists() else {}
    # One switch for scope: EXCLUDE_KINDS=game,social,streaming,format hides those products everywhere.
    exclude = {k for k in os.environ.get("EXCLUDE_KINDS", "").split(",") if k}
    paged = {pid for pid, s in scores.items() if s["distinct_recommenders"] >= MIN_PAGE
             and (products[pid]["kind"] or "") not in exclude}

    is_model = False  # set per product below; quote_obj reads it

    parent_of = family_parents(products, cats)
    tiers = defaultdict(list)
    for child, parent in parent_of.items():
        tiers[parent].append(child)
    rollups = {parent: family_rollup([m for q in [parent, *kids] for m in mentions[q]]) for parent, kids in tiers.items()}
    # Family-wide verdict and themes (family_synth.py) replace those written from plain mentions alone.
    for parent in tiers:
        f = ROOT / "data" / "synth" / "family_v1" / f"{products[parent]['slug']}.json"
        if f.exists():
            o = json.loads(f.read_text())
            if o["verdict"]:
                verdicts[parent] = {"text": " ".join(c["text"] for c in o["verdict"]), "clauses": json.dumps(o["verdict"]),
                                    "use_case_tags": json.dumps(o["use_case_tags"])}
                themes[parent] = [{"polarity": pol, "label": t["label"], "summary": t["summary"], "mention_ids": json.dumps(t["quotes"])}
                                  for pol, key in (("praise", "praise"), ("caveat", "caveats")) for t in o[key]]
    # A family's page exists whenever the family has enough people, even if few people use its bare name.
    paged |= {parent for parent, r in rollups.items() if r["recommenders"] >= MIN_PAGE and (products[parent]["kind"] or "") not in exclude and parent in scores}

    def quote_obj(m):
        return {"id": m["id"], "item_id": m["item_id"], "url": hn(m["item_id"]), "by": m["by"], "date": m["time"][:10],
                "version": version_of(m["raw_name"]) if is_model else None, "stance": m["stance"], "quote": m["reason_quote"] if m["quote_verbatim"] else "",
                "excerpt": " ".join((m["comment"] or "").split())[:420], "context": m["recommender_context"],
                "use_case": m["use_case"], "switched_from": m["switched_from"], "story": m["story_title"]}

    summaries = []
    version_lines = []
    shutil.rmtree(EXPORT, ignore_errors=True)
    EXPORT.mkdir(parents=True)
    shutil.rmtree(DATA / "products", ignore_errors=True)
    (DATA / "products").mkdir(parents=True)
    for pid in paged:
        p, s = products[pid], scores[pid]
        ms = [m for m in mentions[pid] if not m["affiliated"]]
        makers = [m for m in mentions[pid] if m["affiliated"]]
        cat = cats[p["category_id"]]
        sbm = json.loads(s["sentiment_by_month"])
        totals = Counter()
        for v in sbm.values():
            totals.update(v)
        v = verdicts.get(pid)
        good = [m for m in ms if m["stance"] == "recommend" and m["quote_verbatim"] and 50 <= len(m["reason_quote"]) <= 220]
        best = max(good, key=lambda m: (m["confidence"], len(m["reason_quote"])), default=None)
        why = (v["text"].split(". ")[0].rstrip(".") + ".") if v else (best["reason_quote"] if best else "")
        summary = {
            "slug": p["slug"], "name": p["name"], "category": cat["slug"], "category_name": cat["name"],
            "description": p["description"] or "", "recommenders": s["distinct_recommenders"],
            "commenters": s["distinct_commenters"], "warners": s["warners"], "score": s["weighted_score"],
            "rising": s["rising_score"], "first": s["first_mention"][:10], "latest": s["latest_mention"][:10],
            "why": why, "why_is_quote": not v and bool(best), "has_verdict": bool(v),
            "sentiment": {k: totals.get(k, 0) for k in ("recommend", "neutral", "compare", "warn")},
            "spark": [sbm.get(mo, {}).get("recommend", 0) for mo in MONTHS], "listed": s["distinct_recommenders"] >= MIN_LIST,
            "loved": [t["label"] for t in themes[pid] if t["polarity"] == "praise"][:2],
            "caveat": next((t["label"] for t in themes[pid] if t["polarity"] == "caveat"), ""),
            "short_quote": best["reason_quote"] if best else "",
            "kind": p["kind"] or "",
            "icon": (SITE / "public" / "icons" / f"{p['slug']}.ico").exists(),
        }
        if pid in parent_of:
            summary["family"] = products[parent_of[pid]]["slug"]
        if pid in rollups:
            r = rollups[pid]
            sbm = r["months"]
            totals = Counter()
            for mv in sbm.values():
                totals.update(mv)
            summary.update(own_recommenders=s["distinct_recommenders"], recommenders=r["recommenders"], commenters=r["commenters"],
                           warners=r["warners"], first=r["first"], latest=r["latest"], listed=r["recommenders"] >= MIN_LIST,
                           sentiment={k: totals.get(k, 0) for k in ("recommend", "neutral", "compare", "warn")},
                           spark=[sbm.get(mo, {}).get("recommend", 0) for mo in MONTHS],
                           score=s["weighted_score"] + sum(scores[q]["weighted_score"] for q in tiers[pid] if q in scores))
        summaries.append(summary)

        is_model = cat["slug"] == MODELS
        versions, unnamed = version_table(ms) if is_model else ([], 0)
        versions = versions if len(versions) >= 2 else []
        if versions:
            version_lines.append({"slug": p["slug"], "name": p["name"], "recommenders": summary["recommenders"], "unnamed": unnamed,
                                  "family": summary.get("family"), "versions": versions, "icon": summary["icon"]})
        family_ms = [m for q in tiers.get(pid, []) for m in mentions[q]]
        by_id = {m["id"]: m for m in mentions[pid] + family_ms}
        verdict = None
        if v:
            verdict = [{"text": c["text"], "cites": [quote_obj(by_id[i]) for i in c["cites"] if i in by_id]}
                       for c in json.loads(v["clauses"])]
        cited = {q["id"] for c in verdict or [] for q in c["cites"]}
        th = {"praise": [], "caveat": []}
        for t in themes[pid]:
            th[t["polarity"]].append({"label": t["label"], "summary": t["summary"],
                                      "quotes": [quote_obj(by_id[i]) for i in json.loads(t["mention_ids"]) if i in by_id]})
        if not themes[pid]:
            # No synthesis yet: show the strongest verbatim quotes per stance, without labels.
            rec = [m for m in ms if m["stance"] == "recommend" and m["quote_verbatim"] and m["reason_quote"]][:5]
            warn = [m for m in ms if m["stance"] == "warn" and m["quote_verbatim"] and m["reason_quote"]][:4]
            if rec:
                th["praise"].append({"label": "", "summary": "", "quotes": [quote_obj(m) for m in rec]})
            if warn:
                th["caveat"].append({"label": "", "summary": "", "quotes": [quote_obj(m) for m in warn]})
        who, seen = [], set()
        for m in ms:
            if m["recommender_context"] and m["by"] not in seen and m["stance"] in ("recommend", "neutral"):
                seen.add(m["by"])
                who.append({"by": m["by"], "context": m["recommender_context"], "url": hn(m["item_id"]), "date": m["time"][:10]})
        tags = []
        if v:
            tags = [t["tag"] for t in json.loads(v["use_case_tags"] or "[]")]
        uc = Counter(m["use_case"].lower() for m in ms if m["use_case"])
        compared = []
        for q, co, sw_from, sw_to in sorted(comps[pid], key=lambda c: -(c[1] + 2 * (c[2] + c[3])))[:8]:
            if q in paged:
                compared.append({"slug": products[q]["slug"], "name": products[q]["name"], "co": co,
                                 "switched_from": sw_from, "switched_to": sw_to})
        threads = Counter()
        titles = {}
        for m in mentions[pid]:
            if m["story_id"]:
                threads[m["story_id"]] += 1
                titles[m["story_id"]] = m["story_title"]
        # Newest 40 comments, plus enough from each version for its filter to show something.
        shown = {m["id"] for m in ms[:40]}
        by_version = Counter()
        for m in ms:
            v = version_of(m["raw_name"]) if versions else None
            if v in {r["v"] for r in versions} and by_version[v] < 8:
                by_version[v] += 1
                shown.add(m["id"])
        detail = {
            **summary,
            "domain": p["domain"], "github_repo": p["github_repo"], "official_url": official_url(p, official),
            "pricing": p["pricing"] or "", "aliases": [r[0] for r in con.execute("SELECT raw_name FROM alias WHERE product_id = ? ORDER BY n DESC", (pid,))],
            "verdict": verdict, "praise": th["praise"], "caveats": th["caveat"], "who": who[:12], "tags": tags,
            "use_cases": [u for u, _ in uc.most_common(6)], "compared": compared,
            "makers": [quote_obj(m) for m in makers[:6]],
            "threads": [{"url": hn(sid), "title": titles[sid] or f"item {sid}", "mentions": n} for sid, n in threads.most_common(6)],
            "sentiment_by_month": {mo: sbm.get(mo, {}) for mo in MONTHS},
            "versions": versions, "version_unnamed": unnamed,
            "family_ref": ({"slug": products[parent_of[pid]]["slug"], "name": products[parent_of[pid]]["name"]} if pid in parent_of else None),
            "runs_on": next(({"slug": x, "name": products[q]["name"]} for x in [ASSISTANT_OF.get(p["slug"])] if x
                             for q, o in products.items() if o["slug"] == x), None),
            "lines": sorted(({"slug": products[q]["slug"], "name": products[q]["name"], "recommenders": scores[q]["distinct_recommenders"],
                              "icon": (SITE / "public" / "icons" / f"{products[q]['slug']}.ico").exists(),
                              **people_stance([m for m in mentions[q] if not m["affiliated"]])}
                             for q in tiers.get(pid, []) if q in paged), key=lambda x: -x["recommenders"]),
            "quotes": [quote_obj(m) for m in ms if m["id"] in shown or m["id"] in cited] + [quote_obj(m) for m in family_ms if m["id"] in cited],
        }
        (DATA / "products" / f"{p['slug']}.json").write_text(json.dumps(detail))

    # Lists rank by the number they show: distinct recommenders, ties broken by the recency-weighted score.
    summaries.sort(key=lambda s: (-s["recommenders"], -s["score"]))
    home = home_data(con, summaries, mentions, products, themes, verdicts)
    cat_counts = Counter(s["category"] for s in summaries if s["listed"] and not s.get("family"))
    # The family page shows every tier's version table; /models/versions/ groups lines under their family.
    line_by_slug = {x["slug"]: x for x in version_lines}
    pid_by_slug = {o["slug"]: q for q, o in products.items()}
    mentions_of = lambda slug: mentions[pid_by_slug[slug]]
    version_families = []
    for x in summaries:
        if x["category"] != MODELS or x.get("family"):
            continue
        fam_pid = pid_by_slug[x["slug"]]
        lv = latest_of([{"slug": products[q]["slug"], "name": products[q]["name"]} for q in [fam_pid, *tiers.get(fam_pid, [])]], mentions_of)
        if lv:
            x["latest_version"] = lv
        own = line_by_slug.get(x["slug"])
        kids = sorted((l for l in version_lines if l["family"] == x["slug"]), key=lambda l: -l["recommenders"])
        if own or kids:
            version_families.append({"slug": x["slug"], "name": x["name"], "icon": x["icon"], "recommenders": x["recommenders"],
                                     "lines": kids + ([own] if own else [])})
    def update_detail(slug, **fields):
        f = DATA / "products" / f"{slug}.json"
        f.write_text(json.dumps({**json.loads(f.read_text()), **fields}))
    for fam in version_families:
        fields = {"latest_version": next((x["latest_version"] for x in summaries if x["slug"] == fam["slug"] and "latest_version" in x), None)}
        if any(l["slug"] != fam["slug"] for l in fam["lines"]):
            fields["version_groups"] = [l for l in fam["lines"] if l["slug"] != fam["slug"]]
        update_detail(fam["slug"], **fields)
    for line in version_lines:
        if line["family"]:
            update_detail(line["slug"], latest_version=latest_of([line], mentions_of))
    stats = {
        "comments": duckdb.sql(f"SELECT count(DISTINCT id) FROM read_parquet('{ROOT}/data/raw/month=*/*.parquet', union_by_name=true) "
                               "WHERE type = 2 AND time >= '2025-11-01'").fetchall()[0][0],
        "candidates": duckdb.sql(f"SELECT count(*) FROM read_parquet('{ROOT}/data/candidates.parquet')").fetchall()[0][0],
        "with_product": con.execute("SELECT count(*) FROM hn_item").fetchone()[0],
        "with_ranked": con.execute(f"""SELECT count(DISTINCT m.item_id) FROM mention m JOIN product_score s ON s.product_id = m.product_id
                                       WHERE s.distinct_recommenders >= {MIN_LIST} AND m.affiliated = 0""").fetchone()[0],
        "labeled": labeled_count(),
        "mentions": con.execute("SELECT count(*) FROM mention").fetchone()[0],
        "products": len(summaries), "listed": sum(s["listed"] and not s.get("family") for s in summaries),
        "verdicts": len(verdicts), "window": "Nov 2025 to Oct 2026",
    }
    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stats": stats, "months": MONTHS,
        "categories": [{"slug": c["slug"], "name": c["name"], "count": cat_counts[c["slug"]]}
                       for c in sorted(cats.values(), key=lambda c: -cat_counts[c["slug"]]) if cat_counts[c["slug"]]],
        "products": summaries, "home": home,
        "version_families": sorted(version_families, key=lambda x: -x["recommenders"]),
    }
    from compare import Ctx, page_data  # imported here: compare.py uses helpers from this module
    from resolve import name_key
    alias = {}
    for pid_, raw_ in con.execute("SELECT product_id, raw_name FROM alias ORDER BY n"):
        alias[name_key(raw_)] = pid_
    catalog["compare"], compare_quotes = page_data(Ctx(products, cats, mentions, alias), summaries)
    (DATA / "catalog.json").write_text(json.dumps(catalog))
    (EXPORT / "compare-quotes.json").write_text(json.dumps(compare_quotes, separators=(",", ":")))
    listed = [s for s in summaries if s["listed"]]
    # Full dump (minified) for the About page; small per-list files for the JSON buttons.
    (EXPORT / "products.json").write_text(json.dumps({"generated_at": catalog["generated_at"], "stats": stats, "products": listed},
                                                     separators=(",", ":")))

    def compact(items):
        return [{"rank": i + 1, "name": x["name"], "url": f"/p/{x['slug']}/", "category": x["category_name"],
                 "recommenders": x["recommenders"], "warners": x["warners"], "rising": x["rising"],
                 "loved_for": x["loved"], "caveat": x["caveat"]} for i, x in enumerate(items)]
    main_list = [x for x in listed if x["category"] != MODELS]  # AI models are ranked on their own category page
    rising = sorted((x for x in main_list if x["rising"] > 0), key=lambda x: (-x["rising"], -x["score"]))
    (EXPORT / "top.json").write_text(json.dumps(compact(main_list), indent=1))
    (EXPORT / "rising.json").write_text(json.dumps(compact(rising), indent=1))
    (EXPORT / "p").mkdir()
    for s in summaries:
        if s["listed"]:
            shutil.copy(DATA / "products" / f"{s['slug']}.json", EXPORT / "p" / f"{s['slug']}.json")
    for c in catalog["categories"]:
        (EXPORT / f"category-{c['slug']}.json").write_text(json.dumps(compact([s for s in listed if s["category"] == c["slug"] and not s.get("family")]), indent=1))
    # Product URLs that no longer exist because a release rolled up into its line.
    moved = con.execute("""SELECT mp.old_slug, p.slug FROM merged_product mp JOIN product p ON p.id = mp.product_id
                           WHERE mp.users >= 2""").fetchall()
    live = {x["slug"] for x in summaries}
    lines = [f"/p/{o}/ /p/{n}/ 301" for o, n in moved if o not in live and n in live]
    (SITE / "public" / "_redirects").write_text("\n".join(lines) + "\n")
    print(f"published {len(summaries):,} product pages ({stats['listed']:,} listed), {len(catalog['categories'])} categories, "
          f"{stats['verdicts']} verdicts")
    return 0


def labeled_count():
    """Distinct comments the extractor has read (first pass and hard cases), whether or not they named a product."""
    ids = set()
    for f in (ROOT / "data" / "extract").glob("extract_v*/sonnet-*/*.jsonl"):
        for line in f.open():
            r = json.loads(line)
            if not r["missing"]:
                ids.add(r["id"])
    return len(ids)


def best_quote(ms, other_items=None):
    """Highest-confidence verbatim recommend quote, preferring comments that also mention the other product."""
    good = [m for m in ms if m["stance"] == "recommend" and m["quote_verbatim"] and 40 <= len(m["reason_quote"]) <= 240
            and not m["affiliated"]]
    if other_items:
        both = [m for m in good if m["item_id"] in other_items]
        good = both or good
    m = max(good, key=lambda m: (m["confidence"], -abs(len(m["reason_quote"]) - 140)), default=None)
    return m and {"quote": m["reason_quote"], "by": m["by"], "date": m["time"][:10], "url": hn(m["item_id"])}


def home_data(con, summaries, mentions, products, themes, verdicts):
    by_slug = {s["slug"]: s for s in summaries}
    listed = {p["slug"]: pid for pid, p in products.items() if p["slug"] in by_slug and by_slug[p["slug"]]["listed"]}
    # Pick of the day pool: products with a verdict and both a praise and a caveat quote.
    picks = []
    for pid, v in verdicts.items():
        th = themes[pid]
        pq = next((t for t in th if t["polarity"] == "praise"), None)
        cq = next((t for t in th if t["polarity"] == "caveat"), None)
        if not (pq and cq) or products[pid]["slug"] not in by_slug or by_slug[products[pid]["slug"]]["category"] == MODELS:
            continue
        ids = {m["id"]: m for m in mentions[pid]}

        def q(t):
            for i in json.loads(t["mention_ids"]):
                m = ids.get(i)
                if m and m["quote_verbatim"] and m["reason_quote"]:
                    return {"label": t["label"], "quote": m["reason_quote"], "by": m["by"], "date": m["time"][:10], "url": hn(m["item_id"])}
            return None
        praise, caveat = q(pq), q(cq)
        if praise and caveat:
            s = by_slug[products[pid]["slug"]]
            picks.append({"slug": s["slug"], "name": s["name"], "category_name": s["category_name"], "category": s["category"],
                          "description": s["description"], "recommenders": s["recommenders"], "verdict": v["text"],
                          "praise": praise, "caveat": caveat})
    picks.sort(key=lambda p: p["slug"])
    (EXPORT / "picks.json").write_text(json.dumps(picks))  # build-time only; the browser loads one small file per day
    (EXPORT / "picks").mkdir(exist_ok=True)
    for i, pk in enumerate(picks):
        (EXPORT / "picks" / f"{i}.json").write_text(json.dumps(pk))

    # Debates: frequently co-mentioned listed pairs with verdicts on both sides.
    items_of = {pid: {m["item_id"] for m in ms} for pid, ms in mentions.items()}
    rows = con.execute("""SELECT product_a, product_b, co_mentions, switched_a_to_b, switched_b_to_a FROM comparison
                          ORDER BY co_mentions + 3 * (switched_a_to_b + switched_b_to_a) DESC LIMIT 400""").fetchall()
    debates, used, n_ai = [], set(), 0
    for a, b, co, ab, ba in rows:
        if a not in verdicts or b not in verdicts or a in used or b in used:
            continue
        sa, sb = by_slug.get(products[a]["slug"]), by_slug.get(products[b]["slug"])
        if not (sa and sb and sa["listed"] and sb["listed"]) or MODELS in (sa["category"], sb["category"]):
            continue
        is_ai = {sa["category"], sb["category"]} & {"ai", "ai-coding"}
        if is_ai and n_ai >= 2:
            continue
        qa = best_quote(mentions[a], items_of[b])
        qb = best_quote([m for m in mentions[b] if not qa or hn(m["item_id"]) != qa["url"]], items_of[a])
        if not (qa and qb):
            continue
        n_ai += bool(is_ai)
        used |= {a, b}
        debates.append({"co": co, "sides": [
            {"slug": sa["slug"], "name": sa["name"], "recommenders": sa["recommenders"], "warners": sa["warners"], "switched_in": ba, "quote": qa},
            {"slug": sb["slug"], "name": sb["name"], "recommenders": sb["recommenders"], "warners": sb["warners"], "switched_in": ab, "quote": qb}]})
        if len(debates) == 4:
            break

    # Hidden gems: a few recommenders, no warnings, one per category.
    gems, cats_seen = [], set()
    for s in sorted(summaries, key=lambda s: -s["score"]):
        if 3 <= s["recommenders"] <= 8 and s["warners"] == 0 and s["category"] not in cats_seen and s["short_quote"] \
                and s["category"] not in (MODELS, "ai", "ai-coding", "other"):
            gems.append(s["slug"])
            cats_seen.add(s["category"])
        if len(gems) == 6:
            break
    return {"debates": debates, "gems": gems, "pick_count": len(picks)}


if __name__ == "__main__":
    sys.exit(main())
