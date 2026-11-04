"""Stage 4: resolve extracted product names to canonical products and load data/hp.sqlite.

Order of evidence: outbound domain / GitHub repo (when it co-occurs with a name at least twice),
then normalized names. Near-duplicate names among products with 2+ recommenders go to Sonnet,
whose decisions are cached in data/resolve/merges_v1.jsonl. Names whose mentions split across
unrelated categories land in alias_review for a human.
"""
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hp.sqlite"
EXTRACT = ROOT / "data" / "extract" / "extract_v1"
MERGES = ROOT / "data" / "resolve" / "merges_v1.jsonl"
GROUPS = ROOT / "data" / "resolve" / "groups_v1.json"
CANDS = ROOT / "data" / "candidates.parquet"

GENERIC_HOSTS = {"google.com", "apple.com", "microsoft.com", "amazon.com", "github.com", "gitlab.com",
                 "github.io", "npmjs.com", "pypi.org", "crates.io", "docs.rs", "readthedocs.io",
                 "sourceforge.net", "play.google.com", "apps.apple.com", "chromewebstore.google.com",
                 "addons.mozilla.org", "news.ycombinator.com", "huggingface.co", "codeberg.org"}
TWO_LEVEL = {"co.uk", "com.au", "co.jp", "com.br", "co.nz", "org.uk"}
DROP_CATS = re.compile(r"hardware|\bbooks?\b|device|e-reader|laptop|phone|keyboard|headphone|monitor\b(?!ing)")

CATS = [
    ("ai-models", "AI models", []),  # assigned by is_model(), not by keyword
    ("ai-coding", "AI coding tools", ["ai coding", "coding assistant", "coding agent", "code assistant", "code agent", "agentic"]),
    ("ai", "AI tools and apps", ["ai model", "llm", "ai assistant", "chatbot", "language model", "ai", "inference", "speech", "transcription"]),
    ("terminal", "Terminal and shell", ["terminal", "shell", "cli", "command-line", "command line"]),
    ("databases", "Databases", ["database", "db", "sql", "datastore", "key-value", "vector store"]),
    ("git", "Git and code hosting", ["version control", "git", "code hosting", "code review", "forge"]),
    ("infra", "Infrastructure and DevOps", ["container", "orchestration", "infrastructure", "ci", "ci/cd", "deployment", "devops", "hosting", "cloud", "serverless", "paas", "virtualization", "vm", "cdn"]),
    ("observability", "Monitoring and analytics", ["monitoring", "observability", "logging", "analytics", "error tracking"]),
    ("networking", "Networking and VPN", ["vpn", "network", "networking", "dns", "proxy", "firewall", "mesh"]),
    ("security", "Security and passwords", ["password", "security", "authentication", "encryption", "2fa", "identity", "sandbox", "sandboxing"]),
    ("notes", "Notes and knowledge", ["note", "notes", "note-taking", "knowledge", "wiki", "pkm", "writing", "markdown", "documentation"]),
    ("productivity", "Productivity", ["productivity", "task", "todo", "calendar", "project management", "time tracking", "launcher", "issue tracker", "automation"]),
    ("browsers", "Browsers and extensions", ["browser", "extension", "ad block", "adblock", "ad blocker"]),
    ("communication", "Email and chat", ["email", "mail", "chat", "messaging", "communication", "video conferencing", "social"]),
    ("os", "Operating systems and desktop", ["operating system", "os", "linux distribution", "distro", "window manager", "desktop environment", "linux"]),
    ("selfhosted", "Self-hosted and home server", ["self-hosted", "selfhosted", "home server", "nas", "media server", "home automation"]),
    ("dev-libs", "Libraries and frameworks", ["framework", "library", "runtime", "compiler", "build tool", "package manager", "orm", "testing", "web framework", "frontend", "sdk", "toolkit"]),
    ("search", "Search", ["search engine", "search"]),
    ("files", "Files, sync and backup", ["backup", "sync", "storage", "file", "file manager", "cloud storage"]),
    ("media", "Media and creative", ["music", "audio", "video", "photo", "image", "design", "media", "podcast", "reader", "e-book", "ebook", "rss", "graphics", "3d", "cad"]),
    ("finance", "Finance", ["finance", "budget", "budgeting", "accounting", "payment", "banking"]),
    ("editors", "Editors and IDEs", ["editor", "ide"]),
]


# LLMs, model families and chat assistants get their own category and lists, so rankings show the tools built on them.
MODEL_RAW = re.compile(r"(^|\s)(ai|llm|language|image|speech|speech-to-text|coding|video|local|open[- ]weight|foundation|reasoning)"
                       r"(\s.*)?\smodels?$|^(local )?llm$|^large language model|chatbot|^ai assistant$|^ai model")
MODEL_YES = {"whisper", "kokoro"}  # hand-checked exceptions to the rule, both ways
MODEL_NO = {"claudecowork", "grokcode"}


# Named model lines stay separate products (Claude Opus, Gemini Flash, GPT Sol, DeepSeek V4 Pro). Point releases and
# sizes roll up into their line; each mention keeps its original name (mention.raw_name) for the per-version table.
# GPT generations stay apart because they are years apart. Apps and tools aren't model clusters, so they stay out.
MODEL_FAMILIES = [
    (re.compile(r"^(claude[\d.]*)?opus"), "claudeopus", "Claude Opus"),
    (re.compile(r"^(claude[\d.]*)?sonnet"), "claudesonnet", "Claude Sonnet"),
    (re.compile(r"^(claude[\d.]*)?haiku"), "claudehaiku", "Claude Haiku"),
    (re.compile(r"^(claude)?fable"), "claudefable", "Claude Fable"),
    (re.compile(r"^gptoss"), "gptoss", "gpt-oss"),
    (re.compile(r"^gpt5[\d.]*codex"), "gpt5codex", "GPT-5-Codex"),
    (re.compile(r"^gpt6"), "gpt6", "GPT-6"), (re.compile(r"^gpt5"), "gpt5", "GPT-5"),
    (re.compile(r"^gpt4"), "gpt4", "GPT-4"), (re.compile(r"^gpt3"), "gpt3", "GPT-3"),
    (re.compile(r"^gemma"), "gemma", "Gemma"), (re.compile(r"^llama\d"), "llama", "Llama"),
    (re.compile(r"^ds4flash"), "deepseekv4flash", "DeepSeek V4 Flash"),
    (re.compile(r"^glm[\d.]*flash"), "glmflash", "GLM Flash"),
    (re.compile(r"^mistral\d+b"), "mistral", "Mistral"), (re.compile(r"^grok\d"), "grok", "Grok"),
    (re.compile(r"^mimo.*pro"), "mimopro", "MiMo Pro"), (re.compile(r"^(1bit)?bonsai"), "bonsai", "Bonsai"),
]


def merge_model_families(uf, raw, canon_by_root):
    """Fold model clusters into their family. Returns {family key: (display name, [(old name, users), ...])}."""
    clusters = defaultdict(list)
    for m in raw:
        clusters[uf.find(m["nk"])].append(m)
    found = defaultdict(list)
    for root, ms in clusters.items():
        name = canon_by_root.get(root) or Counter(m["name"] for m in ms).most_common(1)[0][0]
        cats = [m["p"].get("category") for m in ms]
        if Counter(map_category(c) for c in cats).most_common(1)[0][0] not in ("ai", "ai-coding") or \
                not is_model({m["nk"] for m in ms}, cats):
            continue
        for rx, key, display in MODEL_FAMILIES:
            if rx.match(name_key(name)):
                found[key].append((root, name, len({m["by"] for m in ms})))
                break
    families = {}
    for key, members in found.items():
        display = next(d for _, k, d in MODEL_FAMILIES if k == key)
        for root, _, _ in members:
            uf.union(key, root)
        families[key] = (display, [(n, u) for _, n, u in members])
        canon_by_root[uf.find(key)] = display
    return families


def is_model(keys, raw_categories):
    if keys & MODEL_NO:
        return False
    if keys & MODEL_YES:
        return True
    return sum(bool(MODEL_RAW.search((c or "").lower().strip())) for c in raw_categories) * 2 >= max(1, len(raw_categories))


def map_category(raw):
    s = (raw or "").lower()
    for slug, _, kws in CATS:
        for kw in kws:
            if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", s):
                return slug
    return "other"


def name_key(name):
    k = name.lower().strip()
    k = re.sub(r"^the\s+", "", k)
    return re.sub(r"[^a-z0-9+#]", "", k)


def domain_key(url):
    if not url:
        return None
    u = url if "://" in url else "https://" + url
    try:
        p = urlparse(u)
    except ValueError:
        return None
    host = (p.hostname or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return None
    if host in ("github.com", "gitlab.com", "codeberg.org"):
        parts = [x for x in p.path.split("/") if x]
        return f"repo:{host}/{parts[0]}/{parts[1]}".lower() if len(parts) >= 2 else None
    labels = host.split(".")
    reg = ".".join(labels[-3:]) if ".".join(labels[-2:]) in TWO_LEVEL else ".".join(labels[-2:])
    if host in GENERIC_HOSTS or reg in GENERIC_HOSTS:
        return None
    return reg


def load_extractions():
    """Later runs override earlier ones for the same comment: first pass, then hard-case re-runs."""
    choice = (ROOT / "data" / "gold" / "choice.txt").read_text().strip()
    runs = {"haiku": ["haiku-low", "sonnet-low"], "sonnet": ["sonnet-low", "sonnet-medium"]}[choice]
    # Compact-prompt labels (e.g. from cloud runs) fill in comments the first pass never read.
    dirs = [EXTRACT.parent / "extract_v3" / "haiku-medium", EXTRACT.parent / "extract_v2" / "sonnet-low"] + \
           [EXTRACT / run for run in runs]
    recs = {}
    for d in dirs:
        for f in d.glob("*.jsonl"):
            for line in f.open():
                r = normalize(json.loads(line), d)
                if r and not r["missing"]:
                    recs[r["id"]] = r
    return recs


def normalize(r, d):
    """Accept extract.py records and raw compact output ({"id", "p": [...]}, or {"id"} alone for no products)."""
    if "id" not in r:
        return None
    if "products" in r:
        return r
    from extract import expand
    return {"id": int(r["id"]), "model": d.name, "prompt": d.parent.name, "missing": False,
            "products": [expand(p) for p in r.get("p", [])]}


# Distinct products that share a domain, a repo or a family name; never merged, whatever the evidence.
KEEP_APART = [("gpt5", "gpt6"), ("pi", "absurd"), ("github", "githubcopilot"), ("zed", "gpui"), ("zed", "deltadb"), ("gpui", "deltadb"),
              ("grapheneos", "vanadium")]

# Model versions roll up to their family; tiers with their own name (DeepSeek V4 Flash, Qwen Code) stay apart.
FAMILIES = [(re.compile(r"^qwen\d"), "qwen", "Qwen"), (re.compile(r"^kimi(k\d.*)?$"), "kimi", "Kimi"),
            (re.compile(r"^glm\d[\d.x]*$"), "glm", "GLM"), (re.compile(r"^deepseekv?\d[\d.]*$"), "deepseek", "DeepSeek")]

# Not software tools in the catalog's narrow sense; tagged so a single switch in publish can hide them.
KINDS = [("game", re.compile(r"(?<![a-z])(game|video game)s?$|game store|game launcher")),
         ("social", re.compile(r"social (network|media)|forum|community")),
         ("streaming", re.compile(r"(music|video) streaming|streaming service|video platform")),
         ("format", re.compile(r"(?<![a-z])(format|protocol|standard|specification|markup language)(?![a-z])"))]


# Hand-checked: real tools whose raw category reads like a format or a social site.
KIND_OVERRIDES = {"ducklake": "", "apacheiceberg": "", "apachearrow": "", "lance": "", "vortex": "", "goat": "",
                  "discourse": "", "flarum": "", "invisionpowerboard": ""}


def apply_kind_overrides(con):
    for pid, name in con.execute("SELECT id, name FROM product").fetchall():
        k = name_key(name)
        if k in KIND_OVERRIDES:
            con.execute("UPDATE product SET kind = ? WHERE id = ?", (KIND_OVERRIDES[k], pid))
    con.commit()


def kind_of(raw_categories):
    c = Counter()
    for raw in raw_categories:
        for kind, rx in KINDS:
            if rx.search((raw or "").lower()):
                c[kind] += 1
                break
    if not c:
        return ""
    kind, n = c.most_common(1)[0]
    return kind if n * 2 >= len(raw_categories) else ""


class UF:
    def __init__(self):
        self.p = {}
        self.members = {}

    def find(self, x):
        self.p.setdefault(x, x)
        self.members.setdefault(x, {x})
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        ma, mb = self.members[ra], self.members[rb]
        for x, y in KEEP_APART:
            if (x in ma and y in mb) or (y in ma and x in mb):
                return
        keep, drop = min(ra, rb), max(ra, rb)
        self.p[drop] = keep
        self.members[keep] = ma | mb
        del self.members[drop]


def lev(a, b):
    if abs(len(a) - len(b)) > 2:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def sonnet_merges(clusters):
    """Ask Sonnet whether near-duplicate names are the same product. Cached per pair."""
    MERGES.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if MERGES.exists():
        for line in MERGES.open():
            d = json.loads(line)
            cache[(d["a"], d["b"])] = d["same"]
    big = {k: c for k, c in clusters.items() if c["users"] >= 2}
    keys = sorted(big)
    pairs = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if b[:3] != a[:3]:
                continue
            if (len(a) >= 4 and b.startswith(a)) or (len(a) >= 6 and lev(a, b) <= 2):
                if (a, b) not in cache:
                    pairs.append((a, b))
    for start in range(0, len(pairs), 120):
        chunk = pairs[start:start + 120]
        lines = []
        for n, (a, b) in enumerate(chunk):
            ca, cb = big[a], big[b]
            lines.append(f"{n}. A=\"{ca['name']}\" ({ca['cat_raw']}; e.g. \"{ca['sample'][:90]}\") | "
                         f"B=\"{cb['name']}\" ({cb['cat_raw']}; e.g. \"{cb['sample'][:90]}\")")
        prompt = ("For each numbered pair of software names extracted from Hacker News comments, decide whether A and B "
                  "refer to the SAME product (spelling variants, abbreviations like Postgres/PostgreSQL, 'X app' vs 'X'). "
                  "Different products from one vendor or family are NOT the same (Claude vs Claude Code, VS Code vs Visual "
                  "Studio, GPT-5 vs GPT-5 Codex, Arc browser vs Arc Linux). Output one JSON object per line: "
                  "{\"n\": <number>, \"same\": true|false}. No other text.\n\n" + "\n".join(lines))
        text, _ = llm.run(prompt, model="sonnet", effort="low")
        got = {int(o["n"]): bool(o["same"]) for o in llm.parse_jsonl(text) if "n" in o}
        with MERGES.open("a") as f:
            for n, (a, b) in enumerate(chunk):
                if n in got:
                    cache[(a, b)] = got[n]
                    f.write(json.dumps({"a": a, "b": b, "same": got[n]}) + "\n")
        print(f"  sonnet merge review: {min(start + 120, len(pairs))}/{len(pairs)} pairs", flush=True)
    return [(a, b) for (a, b), same in cache.items() if same and a in clusters and b in clusters]


def sonnet_groups(clusters):
    """One pass over all names with 2+ commenters: Sonnet lists duplicate groups and a canonical name.
    Catches variants a prefix check misses (VS Code / Visual Studio Code). Cached by the name list."""
    import hashlib
    big = sorted((k for k, c in clusters.items() if c["users"] >= 2), key=lambda k: clusters[k]["name"].lower())
    h = hashlib.sha1("\n".join(big).encode()).hexdigest()[:12]
    cache = json.loads(GROUPS.read_text()) if GROUPS.exists() else {}
    if h not in cache:
        lines = [f"{n}. {clusters[k]['name']} ({clusters[k]['cat_raw']})" for n, k in enumerate(big)]
        prompt = ("Below is a numbered list of software names extracted from Hacker News comments, with a category. "
                  "Find groups of entries that refer to the SAME product: spelling variants, abbreviations (Postgres / "
                  "PostgreSQL, VS Code / Visual Studio Code), and version numbers of the same product or model (Claude "
                  "Opus 4.8 / Opus / Claude Opus). Keep apart different products from one vendor or family (Claude vs "
                  "Claude Code, VS Code vs Visual Studio, Codex vs GPT-5), different tiers (DeepSeek V4 Flash vs DeepSeek "
                  "V4 Pro), and same-named but unrelated products. Output one JSON object per group of 2 or more, one per "
                  "line: {\"same\": [<numbers>], \"name\": \"<canonical product name>\"}. Output nothing for "
                  "entries without duplicates. No other text.\n\n" + "\n".join(lines))
        text, _ = llm.run(prompt, model="sonnet", effort="low", timeout=1800)
        groups = []
        for o in llm.parse_jsonl(text):
            idx = [int(x) for x in o.get("same") or [] if str(x).isdigit() and int(x) < len(big)]
            if len(idx) >= 2:
                groups.append({"keys": [big[i] for i in idx], "name": o.get("name") or ""})
        cache[h] = groups
        GROUPS.parent.mkdir(parents=True, exist_ok=True)
        GROUPS.write_text(json.dumps(cache, indent=1))
        print(f"  sonnet grouping: {len(big)} names, {len(groups)} duplicate groups", flush=True)
    return cache[h]


def slugify(name, used):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("+", "plus").replace("#", "sharp")).strip("-") or "product"
    base, n = s, 2
    while s in used:
        s, n = f"{base}-{n}", n + 1
    used.add(s)
    return s


def main():
    recs = load_extractions()
    cands = duckdb.sql(f"SELECT id, \"by\", time, text, parent, story_id, story_title, parent_text FROM read_parquet('{CANDS}')")
    cmap = {r[0]: r for r in cands.fetchall()}

    raw = []  # one per (comment, product)
    for cid, r in recs.items():
        if cid not in cmap:
            continue
        seen = set()
        for p in r["products"]:
            nk = name_key(p.get("name", ""))
            if not nk or nk in seen:
                continue
            if DROP_CATS.search((p.get("category") or "").lower()):
                continue
            try:
                conf = float(p.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < 0.3:
                continue
            seen.add(nk)
            if len(seen) > 16:
                break
            raw.append({"mid": cid * 16 + len(seen) - 1, "item_id": cid, "by": cmap[cid][1], "name": p["name"].strip(), "nk": nk,
                        "dk": domain_key(p.get("url")), "p": p, "conf": conf})
    print(f"mentions after filters: {len(raw):,} from {len(recs):,} labeled comments")

    uf = UF()
    pair_counts = Counter((m["nk"], m["dk"]) for m in raw if m["dk"])
    by_domain = defaultdict(set)
    for (nk, dk), c in pair_counts.items():
        if c >= 2:
            by_domain[dk].add(nk)
    for dk, nks in by_domain.items():
        # A domain tied to 3+ different names is a vendor hosting several products (claude.com: Claude,
        # Claude Code, ...); merging on it would collapse distinct products, so leave those to name matching.
        if len(nks) >= 3:
            continue
        # Sharing a domain is only evidence when the names agree too (Postgres/PostgreSQL), not Pi/Absurd.
        label = re.sub(r"[^a-z0-9]", "", dk.removeprefix("repo:").split("/")[-1].split(".")[0])
        nks = sorted(nks)
        for i, a in enumerate(nks):
            for b in nks[i + 1:]:
                if a in b or b in a or (len(label) >= 3 and label in a and label in b):
                    uf.union(a, b)
    family_name = {}
    for m in raw:
        uf.find(m["nk"])
        for rx, fk, fname in FAMILIES:
            if rx.match(m["nk"]):
                uf.union(fk, m["nk"])
                family_name[fk] = fname
                break

    def clusters_now():
        cl = defaultdict(list)
        for m in raw:
            cl[uf.find(m["nk"])].append(m)
        out = {}
        for k, ms in cl.items():
            out[k] = {"name": Counter(m["name"] for m in ms).most_common(1)[0][0],
                      "users": len({m["by"] for m in ms}),
                      "cat_raw": Counter((m["p"].get("category") or "") for m in ms).most_common(1)[0][0],
                      "sample": next((m["p"].get("reason_quote") for m in ms if m["p"].get("reason_quote")), "")}
        return out

    for a, b in sonnet_merges(clusters_now()):
        uf.union(a, b)
    canon = {}
    for g in sonnet_groups(clusters_now()):
        for k in g["keys"][1:]:
            uf.union(g["keys"][0], k)
        if g["name"]:
            canon[tuple(g["keys"])] = g["name"]
    canon_by_root = {uf.find(keys[0]): name for keys, name in canon.items()}
    canon_by_root.update({uf.find(fk): fname for fk, fname in family_name.items()})

    families = merge_model_families(uf, raw, canon_by_root)
    family_by_root = {uf.find(k): v for k, v in families.items()}

    groups = defaultdict(list)
    for m in raw:
        groups[uf.find(m["nk"])].append(m)

    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    con.executescript((ROOT / "pipeline" / "schema.sql").read_text())
    con.executemany("INSERT INTO category(id, slug, name) VALUES (?, ?, ?)",
                    [(i + 1, slug, name) for i, (slug, name, _) in enumerate(CATS)] + [(len(CATS) + 1, "other", "Other software")])
    cat_id = {r[0]: r[1] for r in con.execute("SELECT slug, id FROM category")}

    used = set()
    n_review = 0
    for gi, (gk, ms) in enumerate(sorted(groups.items(), key=lambda kv: -len({m["by"] for m in kv[1]}))):
        pid = gi + 1
        name = canon_by_root.get(gk) or Counter(m["name"] for m in ms).most_common(1)[0][0]
        domains = Counter(m["dk"] for m in ms if m["dk"])
        dk = domains.most_common(1)[0][0] if domains else None
        repo = dk[5:] if dk and dk.startswith("repo:") else None
        cats = Counter(map_category(m["p"].get("category")) for m in ms)
        top_cat, top_n = cats.most_common(1)[0]
        if top_cat == "other" and len(cats) > 1:
            top_cat = cats.most_common(2)[1][0]
        if gk in family_by_root or (top_cat in ("ai", "ai-coding") and
                                    is_model({m["nk"] for m in ms}, [m["p"].get("category") for m in ms])):
            top_cat = "ai-models"
        users = len({m["by"] for m in ms})
        if users >= 5 and top_n / len(ms) < 0.5:
            con.execute("INSERT INTO alias_review(raw_name, candidate_product_ids, status, note) VALUES (?, ?, 'open', ?)",
                        (name, json.dumps([pid]), f"categories split: {dict(cats.most_common(4))}"))
            n_review += 1
        slug = slugify(name, used)
        con.execute("""INSERT INTO product(id, slug, name, domain, github_repo, category_id, kind, is_family)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (pid, slug, name, None if repo else dk, repo, cat_id[top_cat],
                     kind_of([m["p"].get("category") for m in ms]), int(gk in family_by_root)))
        for old_name, old_users in family_by_root.get(gk, (None, []))[1]:
            old_slug = slugify(old_name, set())
            if old_slug != slug:
                con.execute("INSERT INTO merged_product(product_id, old_name, old_slug, users) VALUES (?, ?, ?, ?)",
                            (pid, old_name, old_slug, old_users))
        for alias, c in Counter(m["name"] for m in ms).items():
            con.execute("INSERT INTO alias(product_id, raw_name, n) VALUES (?, ?, ?)", (pid, alias, c))
        for m in ms:
            p = m["p"]
            con.execute("""INSERT INTO mention(id, item_id, product_id, raw_name, stance, reason_quote, quote_verbatim,
                           use_case, alternatives, switched_from, recommender_context, affiliated, category_raw,
                           extractor_version, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (m["mid"], m["item_id"], pid, m["name"], p.get("stance") if p.get("stance") in ("recommend", "neutral", "warn", "compare") else "neutral",
                         p.get("reason_quote") or "", int(bool(p.get("quote_verbatim"))), p.get("use_case") or "",
                         json.dumps(p.get("alternatives") or []), p.get("switched_from") or "",
                         p.get("recommender_context") or "", int(bool(p.get("affiliated"))), p.get("category") or "",
                         f"{recs[m['item_id']]['prompt']}/{recs[m['item_id']]['model']}", m["conf"]))
    ids = sorted({m["item_id"] for m in raw})
    con.executemany("INSERT INTO hn_item(id, by, time, text, parent, story_id, story_title, parent_text) VALUES (?,?,?,?,?,?,?,?)",
                    [(i, cmap[i][1], cmap[i][2].isoformat(), cmap[i][3], cmap[i][4], cmap[i][5], cmap[i][6], cmap[i][7]) for i in ids])
    apply_kind_overrides(con)
    con.execute("INSERT INTO mention_fts(rowid, product, quote, context) SELECT m.id, p.name, m.reason_quote, m.recommender_context FROM mention m JOIN product p ON p.id = m.product_id")
    con.commit()
    n_prod = con.execute("SELECT count(*) FROM product").fetchone()[0]
    n2 = con.execute("SELECT count(*) FROM (SELECT product_id FROM mention JOIN hn_item i ON i.id = item_id GROUP BY 1 HAVING count(DISTINCT i.by) >= 3)").fetchone()[0]
    print(f"products: {n_prod:,} ({n2:,} with 3+ distinct commenters); mentions: {len(raw):,}; alias review queue: {n_review}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
