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
CANDS = ROOT / "data" / "candidates.parquet"

GENERIC_HOSTS = {"google.com", "apple.com", "microsoft.com", "amazon.com", "github.com", "gitlab.com",
                 "github.io", "npmjs.com", "pypi.org", "crates.io", "docs.rs", "readthedocs.io",
                 "sourceforge.net", "play.google.com", "apps.apple.com", "chromewebstore.google.com",
                 "addons.mozilla.org", "news.ycombinator.com", "huggingface.co", "codeberg.org"}
TWO_LEVEL = {"co.uk", "com.au", "co.jp", "com.br", "co.nz", "org.uk"}
DROP_CATS = re.compile(r"hardware|\bbooks?\b|device|e-reader|laptop|phone|keyboard|headphone|monitor\b(?!ing)")

CATS = [
    ("ai-coding", "AI coding tools", ["ai coding", "coding assistant", "coding agent", "code assistant", "code agent", "agentic"]),
    ("ai", "AI models and assistants", ["ai model", "llm", "ai assistant", "chatbot", "language model", "ai", "inference", "speech", "transcription"]),
    ("editors", "Editors and IDEs", ["editor", "ide"]),
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
]


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
    recs = {}
    for run in runs:
        for f in (EXTRACT / run).glob("*.jsonl"):
            for line in f.open():
                r = json.loads(line)
                if not r["missing"]:
                    recs[r["id"]] = r
    return recs


class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


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
        nks = sorted(nks)
        for other in nks[1:]:
            uf.union(nks[0], other)
    for m in raw:
        uf.find(m["nk"])

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
        name = Counter(m["name"] for m in ms).most_common(1)[0][0]
        domains = Counter(m["dk"] for m in ms if m["dk"])
        dk = domains.most_common(1)[0][0] if domains else None
        repo = dk[5:] if dk and dk.startswith("repo:") else None
        cats = Counter(map_category(m["p"].get("category")) for m in ms)
        top_cat, top_n = cats.most_common(1)[0]
        if top_cat == "other" and len(cats) > 1:
            top_cat = cats.most_common(2)[1][0]
        users = len({m["by"] for m in ms})
        if users >= 5 and top_n / len(ms) < 0.5:
            con.execute("INSERT INTO alias_review(raw_name, candidate_product_ids, status, note) VALUES (?, ?, 'open', ?)",
                        (name, json.dumps([pid]), f"categories split: {dict(cats.most_common(4))}"))
            n_review += 1
        con.execute("INSERT INTO product(id, slug, name, domain, github_repo, category_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, slugify(name, used), name, None if repo else dk, repo, cat_id[top_cat]))
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
                         f"extract_v1/{recs[m['item_id']]['model']}", m["conf"]))
    ids = sorted({m["item_id"] for m in raw})
    con.executemany("INSERT INTO hn_item(id, by, time, text, parent, story_id, story_title, parent_text) VALUES (?,?,?,?,?,?,?,?)",
                    [(i, cmap[i][1], cmap[i][2].isoformat(), cmap[i][3], cmap[i][4], cmap[i][5], cmap[i][6], cmap[i][7]) for i in ids])
    con.execute("INSERT INTO mention_fts(rowid, product, quote, context) SELECT m.id, p.name, m.reason_quote, m.recommender_context FROM mention m JOIN product p ON p.id = m.product_id")
    con.commit()
    n_prod = con.execute("SELECT count(*) FROM product").fetchone()[0]
    n2 = con.execute("SELECT count(*) FROM (SELECT product_id FROM mention JOIN hn_item i ON i.id = item_id GROUP BY 1 HAVING count(DISTINCT i.by) >= 3)").fetchone()[0]
    print(f"products: {n_prod:,} ({n2:,} with 3+ distinct commenters); mentions: {len(raw):,}; alias review queue: {n_review}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
