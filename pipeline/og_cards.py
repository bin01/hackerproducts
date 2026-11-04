"""Social preview cards (1200x630) for the site, /models/compare/ and /models/versions/, from the published catalog."""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "public" / "og"
FONT = "/System/Library/Fonts/Supplemental/Verdana.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Verdana Bold.ttf"
BG, TEXT, MUTED, ACCENT, RULE, LOVE, HATE, MID = "#f6f6ef", "#1d1d1b", "#6e6d66", "#ff6600", "#e2e1d6", "#1c5cab", "#c23b3a", "#d8d7cf"


def f(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


def base(title, sub):
    im = Image.new("RGB", (1200, 630), BG)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 1200, 10), fill=ACCENT)
    d.rounded_rectangle((70, 58, 118, 106), 6, fill=ACCENT)
    d.text((94, 82), "P", font=f(32, True), fill="white", anchor="mm")
    d.text((134, 82), "HackerProducts", font=f(28, True), fill=TEXT, anchor="lm")
    d.text((70, 150), title, font=f(56, True), fill=TEXT)
    d.text((70, 232), sub, font=f(25), fill=MUTED)
    return im, d


def foot(d, text):
    d.text((70, 566), text, font=f(20), fill=MUTED)


def site(cat):
    s = cat["stats"]
    im, d = base("What Hacker News", "")
    d.text((70, 222), "actually uses", font=f(56, True), fill=TEXT)
    d.text((70, 318), "Software recommended in HN comments,", font=f(30), fill=MUTED)
    d.text((70, 362), "with the comments that say why.", font=f(30), fill=MUTED)
    foot(d, f"{s['listed']:,} products · {s['comments']:,} comments scanned · {s['window']}")
    im.save(OUT / "site.png", optimize=True)


def compare(cat):
    c = cat["compare"]
    im, d = base("AI models compared", "Best for each job, head to head, and who's switching")
    rows = c["table"][:5]
    top = max(r["recommenders"] for r in rows)
    y = 300
    for r in rows:
        d.text((70, y), r["name"], font=f(26, True), fill=TEXT, anchor="lm")
        w = 720 * r["recommenders"] / top
        d.rounded_rectangle((300, y - 13, 300 + max(w, 8), y + 13), 5, fill=ACCENT)
        d.text((300 + w + 14, y), f"{r['recommenders']:,}", font=f(24), fill=MUTED, anchor="lm")
        y += 50
    foot(d, f"People recommending each family in Hacker News comments, {cat['stats']['window']}")
    im.save(OUT / "compare.png", optimize=True)


def versions(cat):
    def top(is_open):
        seen, rows = set(), []
        for x in sorted((x for x in cat["compare"]["releases"] if x["cur"] and x["o"] == is_open), key=lambda x: -x["r"] / (x["r"] + x["w"])):
            if x["f"] not in seen:
                seen.add(x["f"])
                rows.append(x)
        return rows[:3]
    im, d = base("Which AI models are loved?", "What HN commenters say about the releases out now")
    for col, (label, rows) in enumerate((("Closed models", top(False)), ("Open weights", top(True)))):
        x0 = 70 + col * 545
        d.text((x0, 296), label, font=f(22, True), fill=MUTED, anchor="lm")
        y = 334
        for x in rows:
            love = x["r"] / (x["r"] + x["w"])
            d.text((x0, y), x["l"], font=f(23, True), fill=TEXT, anchor="lm")
            d.rounded_rectangle((x0, y + 18, x0 + 400, y + 30), 4, fill=MID)
            d.rounded_rectangle((x0, y + 18, x0 + 400 * love, y + 30), 4, fill=LOVE)
            d.text((x0 + 412, y + 24), f"{round(love * 100)}%", font=f(22), fill=MUTED, anchor="lm")
            y += 66
    foot(d, "Love score: recommend ÷ (recommend + warn), while each release was the newest")
    im.save(OUT / "versions.png", optimize=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cat = json.loads((ROOT / "site" / "src" / "data" / "catalog.json").read_text())
    site(cat)
    compare(cat)
    versions(cat)
    print("wrote", [p.name for p in sorted(OUT.glob("*.png"))])


if __name__ == "__main__":
    main()
