"""Pick 20 random Roblox usernames a day from @kalemod comments.

Pulls the last LOOKBACK_DAYS of comments with the YouTube Data API, finds the
commenter's own Roblox username with Claude Haiku or Gemini (pattern matching if
neither key is set),
and picks DAILY random usernames nobody has picked before. Picked usernames
stay out until you run with --reset.

State lives in data/*.json and picks/*.md, committed by the GitHub Action.
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
PICKS = ROOT / "picks"
CANDIDATES_FILE = DATA / "candidates.json"
SCANNED_FILE = DATA / "scanned_comments.json"

CHANNEL_ID = os.environ.get("CHANNEL_ID", "UCpPOMaPJI9KmWKjkQw6XzdQ")  # @kalemod
DAILY = int(os.environ.get("DAILY", "20"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "30"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "200"))  # 1 quota unit each, 10,000/day free
LOCAL_TZ = timezone(timedelta(hours=8))  # Cebu
HAIKU_MODEL = os.environ.get("HAIKU_MODEL", "claude-haiku-5-5")
BATCH = 150

YT_KEY = os.environ.get("YT_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
GEMINI_GAP_SECONDS = 7  # stay under the free tier's requests-per-minute limit

UNAME = re.compile(r"^(?=.{3,20}$)[A-Za-z0-9]+(?:_[A-Za-z0-9]+)?$")


# ---------- storage ----------

def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


# ---------- YouTube ----------

def yt_get(endpoint, params):
    params = {**params, "key": YT_KEY}
    r = requests.get(f"https://www.googleapis.com/youtube/v3/{endpoint}", params=params, timeout=30)
    if r.status_code != 200:
        sys.exit(f"YouTube API error {r.status_code}: {r.text[:500]}")
    return r.json()


def fetch_comments(since_iso):
    """All non-owner comments (top-level + returned replies) newer than since_iso."""
    out, token, pages = [], None, 0
    while pages < MAX_PAGES:
        params = {
            "part": "snippet,replies",
            "allThreadsRelatedToChannelId": CHANNEL_ID,
            "order": "time",
            "maxResults": 100,
            "textFormat": "plainText",
        }
        if token:
            params["pageToken"] = token
        data = yt_get("commentThreads", params)
        pages += 1
        reached_old = False
        for thread in data.get("items", []):
            top = thread["snippet"]["topLevelComment"]
            video_id = thread["snippet"].get("videoId", "")
            items = [top] + thread.get("replies", {}).get("comments", [])
            for c in items:
                s = c["snippet"]
                published = s.get("publishedAt", "")
                if published <= since_iso:
                    continue
                author_channel = (s.get("authorChannelId") or {}).get("value")
                if author_channel == CHANNEL_ID:
                    continue
                text = (s.get("textOriginal") or s.get("textDisplay") or "").strip()
                if text:
                    out.append({
                        "id": c["id"],
                        "text": " ".join(text.split()),
                        "author": s.get("authorDisplayName", ""),
                        "published": published,
                        "video_id": video_id,
                    })
            if top["snippet"].get("publishedAt", "") <= since_iso:
                reached_old = True
        token = data.get("nextPageToken")
        if not token or reached_old:
            break
    print(f"Read {pages} page(s) of comment threads, {len(out)} comments in window.")
    return out


def video_titles(ids):
    titles = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), 50):
        data = yt_get("videos", {"part": "snippet", "id": ",".join(ids[i:i + 50])})
        for v in data.get("items", []):
            titles[v["id"]] = v["snippet"]["title"]
    return titles


# ---------- username detection ----------

PROMPT = (
    "These are YouTube comments on Kalemod, an animated Shorts channel. Viewers often post their own Roblox "
    "username hoping to be added to a video (e.g. \"my roblox is X\", \"add me: X\", \"im X plss\", \"user: X\", "
    "or a username followed by a plea like \"still waiting\").\n"
    "Return the commenter's OWN Roblox username for each comment that shares one. Skip character names, "
    "celebrities, other people's names, ordinary words, and comments that only ask to be added without giving a "
    "name. A Roblox username is 3-20 letters/digits with at most one underscore, and must appear in the comment "
    "exactly as written.\n"
    "Reply with only a JSON array like [{\"i\": 4, \"username\": \"Cool_Kid123\"}]. Reply [] if none.\n\n"
    "Comments (index TAB text):\n"
)

PATTERN = re.compile(
    r"(?:roblox|rblx|user\s*name|username|user|add me|my name is|i'?m|im)\s*(?:is|:|-|=)?\s*@?([A-Za-z0-9_]{3,20})",
    re.I,
)


def ask_haiku(batch):
    lines = "\n".join(f"{i}\t{c['text'][:300]}" for i, c in enumerate(batch))
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": HAIKU_MODEL, "max_tokens": 2000, "messages": [{"role": "user", "content": PROMPT + lines}]},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Anthropic API error {r.status_code}: {r.text[:300]}")
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    m = re.search(r"\[.*\]", text, re.S)
    try:
        return json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return []


gemini_model = GEMINI_MODEL
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def pick_gemini_model(exclude):
    """Find a current Flash model this key can use, newest first."""
    r = requests.get(f"{GEMINI_BASE}/models", params={"key": GEMINI_KEY, "pageSize": 200}, timeout=30)
    if r.status_code != 200:
        return None
    names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    flash = [n for n in names if "flash" in n and n not in exclude
             and not any(x in n for x in ("lite", "image", "tts", "live", "audio", "thinking", "exp"))]

    def version(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return (float(m.group(1)) if m else 0, "preview" not in n, -len(n))

    flash.sort(key=version, reverse=True)
    return flash[0] if flash else None


def ask_gemini(batch):
    global gemini_model
    lines = "\n".join(f"{i}\t{c['text'][:300]}" for i, c in enumerate(batch))
    body = {
        "contents": [{"parts": [{"text": PROMPT + lines}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    tried = set()
    attempt = 0
    while True:
        url = f"{GEMINI_BASE}/models/{gemini_model}:generateContent"
        r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=120)
        if r.status_code == 200:
            break
        if r.status_code == 404 and gemini_model not in tried:
            tried.add(gemini_model)
            nxt = pick_gemini_model(tried)
            if nxt:
                print(f"Gemini model {gemini_model} isn't available, switching to {nxt}.")
                gemini_model = nxt
                continue
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 4:
            attempt += 1
            time.sleep(20 * attempt)
            continue
        sys.exit(f"Gemini API error {r.status_code} ({gemini_model}): {r.text[:500]}")
    parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    m = re.search(r"\[.*\]", text, re.S)
    try:
        return json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return []


use_claude = bool(ANTHROPIC_KEY)


def find_usernames(comments):
    """Returns {comment_id: username or None} for every comment given."""
    global use_claude
    result = {c["id"]: None for c in comments}
    method = "Claude Haiku" if use_claude else "Gemini" if GEMINI_KEY else "pattern matching"
    print(f"Finding usernames with {method}.")
    for start in range(0, len(comments), BATCH):
        batch = comments[start:start + BATCH]
        hits = None
        if use_claude:
            try:
                hits = ask_haiku(batch)
            except RuntimeError as e:
                use_claude = False
                print(f"{e}\nClaude isn't usable, switching to {'Gemini' if GEMINI_KEY else 'pattern matching'}.")
        if hits is not None:
            pass
        elif GEMINI_KEY:
            if start:
                time.sleep(GEMINI_GAP_SECONDS)
            hits = ask_gemini(batch)
        else:
            hits = []
            for i, c in enumerate(batch):
                m = PATTERN.search(c["text"])
                if m:
                    hits.append({"i": i, "username": m.group(1)})
        for h in hits:
            try:
                c = batch[int(h.get("i"))]
            except (TypeError, ValueError, IndexError):
                continue
            u = str(h.get("username") or "").lstrip("@").strip()
            if UNAME.match(u) and u.lower() in c["text"].lower():
                result[c["id"]] = u
        print(f"Checked {min(start + BATCH, len(comments))}/{len(comments)} new comments for usernames.")
    return result


# ---------- main ----------

def write_day(day, names, titles):
    lines = [f"# Kalemod Roblox picks for {day}", "", f"{len(names)} usernames.", ""]
    lines += [f"{i}. `{p['username']}`" for i, p in enumerate(names, 1)]
    lines += ["", "## Where they came from", ""]
    for p in names:
        title = titles.get(p.get("video_id"), "")
        snippet = p.get("snippet", "").replace("|", "/")
        lines.append(f"- **{p['username']}** ({p.get('author', '')}{', ' + title if title else ''}): {snippet}")
    body = "\n".join(lines) + "\n"
    (PICKS / f"{day}.md").write_text(body, encoding="utf-8")
    (PICKS / "latest.md").write_text(body, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="make every picked username eligible again")
    ap.add_argument("--no-pick", action="store_true", help="scan only, don't pick")
    args = ap.parse_args()

    candidates = load(CANDIDATES_FILE, {})

    if args.reset:
        n = sum(1 for c in candidates.values() if c.get("picked_at"))
        for c in candidates.values():
            c["picked_at"] = None
        save(CANDIDATES_FILE, candidates)
        print(f"Reset picked history for {n} usernames.")

    if not YT_KEY:
        sys.exit("YT_API_KEY is not set.")

    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Pull comments in the window; only send never-checked ones to Claude.
    scanned = load(SCANNED_FILE, {})  # comment_id -> published
    comments = fetch_comments(window_start)
    new = [c for c in comments if c["id"] not in scanned]
    total_found = 0
    CHUNK = BATCH * 10  # save progress every 10 batches

    def merge(chunk, found):
        nonlocal_found = 0
        for c in chunk:
            scanned[c["id"]] = c["published"]
            u = found.get(c["id"])
            if not u:
                continue
            nonlocal_found += 1
            key = u.lower()
            cur = candidates.get(key)
            if cur and cur.get("last_mentioned_at", "") >= c["published"]:
                continue
            candidates[key] = {
                "username": u,
                "author": c["author"],
                "snippet": c["text"][:160],
                "video_id": c["video_id"],
                "last_mentioned_at": c["published"],
                "first_seen_at": (cur or {}).get("first_seen_at") or now.isoformat(),
                "picked_at": (cur or {}).get("picked_at"),
                "pick_count": (cur or {}).get("pick_count", 0),
            }
        return nonlocal_found

    for i in range(0, len(new), CHUNK):
        chunk = new[i:i + CHUNK]
        total_found += merge(chunk, find_usernames(chunk))
        # save progress so a failure later doesn't redo this chunk
        save(SCANNED_FILE, {k: v for k, v in scanned.items() if v > window_start})
        save(CANDIDATES_FILE, candidates)
    print(f"{total_found} usernames found in {len(new)} new comments.")

    # 2. Pick.
    if not args.no_pick:
        day = now.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
        day_file = DATA / "days" / f"{day}.json"
        today = load(day_file, [])
        need = DAILY - len(today)
        taken = {p["username"].lower() for p in today}
        pool = [k for k, c in candidates.items()
                if k not in taken and not c.get("picked_at") and c.get("last_mentioned_at", "") > window_start]
        random.shuffle(pool)
        chosen = pool[:max(need, 0)]
        for k in chosen:
            c = candidates[k]
            c["picked_at"] = now.isoformat()
            c["pick_count"] = c.get("pick_count", 0) + 1
            today.append({"username": c["username"], "author": c["author"], "snippet": c["snippet"],
                          "video_id": c["video_id"], "repeat": c["pick_count"] > 1})
        save(day_file, today)
        if chosen or not (PICKS / f"{day}.md").exists():
            PICKS.mkdir(exist_ok=True)
            write_day(day, today, video_titles([p.get("video_id") for p in today]))
        print(f"Picked {len(chosen)} today ({len(today)}/{DAILY}). {len(pool) - len(chosen)} still unpicked.")
        if len(today) < DAILY:
            print("Not enough unpicked usernames left. Run the workflow with reset to start over.")

    save(CANDIDATES_FILE, candidates)


if __name__ == "__main__":
    main()
