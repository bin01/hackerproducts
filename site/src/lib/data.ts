import catalog from "../data/catalog.json";

export type Summary = (typeof catalog.products)[number];

const details = import.meta.glob("../data/products/*.json", { eager: true, import: "default" });

export function product(slug: string): any {
  return details[`../data/products/${slug}.json`];
}

export const products: Summary[] = catalog.products;
export const listed = products.filter((p) => p.listed);
export const categories = catalog.categories;
export const stats = catalog.stats;
export const months = catalog.months;

export const rising = [...listed]
  .filter((p) => p.rising > 0)
  .sort((a, b) => b.rising - a.rising || b.score - a.score);

export function monthLabel(m: string) {
  const [y, mo] = m.split("-");
  return new Date(Number(y), Number(mo) - 1, 1).toLocaleString("en", { month: "short" }) + " " + y.slice(2);
}

export function fmtDate(d: string) {
  return new Date(d + "T00:00:00Z").toLocaleDateString("en", { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}

const compact = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}K` : n.toLocaleString("en");
export const counts = {
  scanned: compact(stats.comments),
  candidates: compact(stats.candidates),
  read: stats.labeled.toLocaleString("en"),
};

export function toMarkdown(title: string, items: Summary[], base: string, start = 1) {
  return [`## ${title} (HackerProducts)`, "", ...items.map((p, i) => {
    const hook = p.loved.length ? `Loved for ${p.loved.join(", ")}.` : "";
    return `${start + i}. [${p.name}](${base}/p/${p.slug}/): ${p.recommenders} HN recommenders. ${hook}`.trim();
  })].join("\n");
}

export const PAGE_SIZE = 100;
