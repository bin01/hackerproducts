You label Hacker News comments for a catalog of SOFTWARE that HN commenters recommend.

For each comment below, list every specific, named software product the commenter expresses a stance on.
Software means: apps, SaaS, developer tools, libraries, frameworks, databases, OSes, browsers, editors,
self-hosted projects, CLIs, AI models and assistants. Do not list programming languages (Python, Rust, etc.).
Skip hardware (laptops, phones, e-readers), books, websites that are just content, people, companies in
general (e.g. "Google" as a company), and generic terms ("LLMs", "AI", "a password manager", "Linux distros").
A specific model family counts as a product only if named ("Claude", "GPT-5", "DeepSeek V4"). Skip products only mentioned in passing with
no stance. Do not invent products that are not named in the comment.

For each product give:
- name: the product's canonical name as commonly written (e.g. "Obsidian", "PostgreSQL", "Tailscale")
- url: a URL or domain for this product if the comment itself contains one, else ""
- stance: "recommend" (uses/endorses it), "neutral" (uses it, no clear endorsement), "warn" (advises
  against it or reports serious problems), or "compare" (weighs it against alternatives without a verdict)
- reason_quote: the shortest VERBATIM span (max ~200 chars) from the comment that shows the stance. Copy
  exactly, character for character. "" if none.
- use_case: what they use it for, in 2 to 6 words, only if stated or obvious from the comment, else ""
- alternatives: other products named as alternatives or compared, as a list of names
- switched_from: the product they moved away from to this one, if stated, else ""
- recommender_context: who the commenter says they are or their situation, ONLY if stated in the comment
  ("solo founder", "run a 20-person team", "SRE at a bank", "Linux user"), else ""
- affiliated: true if the commenter says or clearly implies they built, work on, or sell it ("I made",
  "we built", "I work at", "our product", "disclosure"), else false
- category: a short software category, e.g. "Note-taking", "Database", "Password manager", "Code editor",
  "VPN / networking", "Self-hosted", "Terminal", "Browser", "AI coding assistant", "Email", "Monitoring"
- confidence: 0.0 to 1.0, how sure you are about the product and stance (sarcasm, ambiguous names like
  "Arc" or "Linear" without context, or unclear stance lower it)

Output exactly one JSON object per comment, one per line (JSONL), in input order, no prose, no code fences:
{"id": <comment id>, "products": [ {...}, ... ]}
Use "products": [] when the comment names no qualifying software with a stance.

COMMENTS:
