"""Run a prompt through headless Claude Code on the user's subscription (no API key)."""
import json
import os
import subprocess
import time

MODELS = {"haiku": "haiku", "sonnet": "sonnet", "opus": "opus"}
LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit", "5-hour limit", "weekly limit", "out of extra usage")


class UsageLimit(Exception):
    pass


def run(prompt, model="haiku", effort="low", system="You are a careful data labeler. Output only what is asked.",
        timeout=900, retries=3, thinking=True):
    cmd = ["claude", "-p", "--model", MODELS[model], "--effort", effort, "--system-prompt", system,
           "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
           "--output-format", "json"]
    last = None
    for attempt in range(retries):
        try:
            env = None if thinking else {**os.environ, "MAX_THINKING_TOKENS": "0"}
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd="/tmp", env=env)
        except subprocess.TimeoutExpired:
            last = "timeout"
            continue
        out = p.stdout.strip()
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            d = {"is_error": True, "result": out or p.stderr}
        text = str(d.get("result", ""))
        if d.get("is_error") or p.returncode != 0:
            blob = (text + " " + p.stderr).lower()
            if any(m in blob for m in LIMIT_MARKERS):
                raise UsageLimit(text[:300] or p.stderr[:300])
            last = text[:300] or p.stderr[:300]
            time.sleep(5 * (attempt + 1))
            continue
        return text, d.get("usage", {})
    raise RuntimeError(f"claude -p failed after {retries} tries: {last}")


def parse_jsonl(text):
    """Pull JSON objects out of model output, one per line, tolerating fences and stray prose."""
    rows = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
