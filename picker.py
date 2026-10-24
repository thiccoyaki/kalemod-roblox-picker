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
BATCH = 200

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

class YouTubeError(Exception):
    pass


def yt_get(endpoint, params, retries=3):
    """GET a YouTube Data API endpoint, retrying YouTube's flaky 400/5xx errors."""
    params = {**params, "key": YT_KEY}
    for attempt in range(retries + 1):
        try:
            r = requests.get(f"https://www.googleapis.com/youtube/v3/{endpoint}", params=params, timeout=30)
        except requests.RequestException as e:
            err = f"network error: {e}"
        else:
            if r.status_code == 200:
                return r.json()
            err = f"{r.status_code}: {r.text[:300]}"
            # 403 = bad key / quota, 404 = missing; retrying won't help
            if r.status_code in (401, 403, 404):
                sys.exit(f"YouTube API error {err}")
        if attempt < retries:
            time.sleep(5 * (attempt + 1))
    raise YouTubeError(err)


def _collect(thread, since_iso, out, seen):
    top = thread["snippet"]["topLevelComment"]
    video_id = thread["snippet"].get("videoId", "")
    for c in [top] + thread.get("replies", {}).get("comments", []):
        s = c["snippet"]
        published = s.get("publishedAt", "")
        if published <= since_iso or c["id"] in seen:
            continue
        if (s.get("authorChannelId") or {}).get("value") == CHANNEL_ID:
            continue
        text = (s.get("textOriginal") or s.get("textDisplay") or "").strip()
        if text:
            seen.add(c["id"])
            out.append({
                "id": c["id"],
                "text": " ".join(text.split()),
                "author": s.get("authorDisplayName", ""),
                "published": published,
                "video_id": video_id,
            })
    return top["snippet"].get("publishedAt", "")


def _walk_threads(base_params, since_iso, out, seen, budget):
    """Page newest-first through commentThreads. Returns pages used. Raises YouTubeError."""
    token, pages = None, 0
    while pages < budget:
        params = {**base_params, "part": "snippet,replies", "order": "time",
                  "maxResults": 100, "textFormat": "plainText"}
        if token:
            params["pageToken"] = token
        data = yt_get("commentThreads", params)
        pages += 1
        reached_old = False
        for thread in data.get("items", []):
            if _collect(thread, since_iso, out, seen) <= since_iso:
                reached_old = True
        token = data.get("nextPageToken")
        if not token or reached_old:
            break
    return pages


def recent_video_ids(since_iso):
    """Videos uploaded since since_iso, from the channel's uploads playlist."""
    uploads = "UU" + CHANNEL_ID[2:]
    ids, token = [], None
    while True:
        params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = yt_get("playlistItems", params)
        done = False
        for it in data.get("items", []):
            when = it["contentDetails"].get("videoPublishedAt", "")
            if when and when <= since_iso:
                done = True
                continue
            ids.append(it["contentDetails"]["videoId"])
        token = data.get("nextPageToken")
        if done or not token:
            return ids


def fetch_comments(since_iso):
    """All non-owner comments (top-level + returned replies) newer than since_iso."""
    out, seen = [], set()
    try:
        pages = _walk_threads({"allThreadsRelatedToChannelId": CHANNEL_ID}, since_iso, out, seen, MAX_PAGES)
        print(f"Read {pages} page(s) of channel comment threads, {len(out)} comments in window.")
        return out
    except YouTubeError as e:
        print(f"YouTube's channel-wide comment feed failed after {len(out)} comments ({e}).")
        print("Switching to reading each recent video's comments.")

    videos = recent_video_ids(since_iso)
    print(f"{len(videos)} videos uploaded in the window.")
    failed = 0
    for n, vid in enumerate(videos, 1):
        try:
            _walk_threads({"videoId": vid}, since_iso, out, seen, MAX_PAGES)
        except YouTubeError as e:
            failed += 1
            print(f"Skipped video {vid}: {e}")
        if n % 10 == 0:
            print(f"Read comments on {n}/{len(videos)} videos, {len(out)} comments so far.")
    print(f"Done: {len(out)} comments in window ({failed} video(s) skipped).")
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


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
gemini_model = None          # chosen on first use
gemini_exhausted = set()     # models whose daily free quota ran out this run


class AIQuotaExhausted(Exception):
    pass


def gemini_models():
    """Models this key can call, best first: newest Flash-Lite (fast, bigger free quota), then Flash."""
    r = requests.get(f"{GEMINI_BASE}/models", params={"key": GEMINI_KEY, "pageSize": 200}, timeout=30)
    if r.status_code != 200:
        return []
    names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    bad = ("image", "tts", "live", "audio", "thinking", "exp", "embedding")
    usable = [n for n in names if "flash" in n and not any(x in n for x in bad)]

    def rank(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return ("lite" in n, float(m.group(1)) if m else 0, "preview" not in n, -len(n))

    return sorted(usable, key=rank, reverse=True)


def next_gemini_model():
    global gemini_model
    if os.environ.get("GEMINI_MODEL") and os.environ["GEMINI_MODEL"] not in gemini_exhausted:
        gemini_model = os.environ["GEMINI_MODEL"]
        return gemini_model
    for m in gemini_models() or [GEMINI_MODEL]:
        if m not in gemini_exhausted:
            gemini_model = m
            return m
    gemini_model = None
    return None


def ask_gemini(batch):
    global gemini_model
    if gemini_model is None and not next_gemini_model():
        raise AIQuotaExhausted("no Gemini model left with free quota today")
    lines = "\n".join(f"{i}\t{c['text'][:300]}" for i, c in enumerate(batch))
    body = {
        "contents": [{"parts": [{"text": PROMPT + lines}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    attempt = 0
    while True:
        url = f"{GEMINI_BASE}/models/{gemini_model}:generateContent"
        try:
            r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=180)
        except requests.RequestException as e:
            if attempt < 5:
                attempt += 1
                print(f"Gemini request failed ({type(e).__name__}), retrying in {15 * attempt}s.")
                time.sleep(15 * attempt)
                continue
            raise AIQuotaExhausted(f"Gemini kept timing out: {e}")
        if r.status_code == 200:
            print(f"Gemini {gemini_model} answered in {r.elapsed.total_seconds():.0f}s.")
            break
        daily = r.status_code == 429 and ("PerDay" in r.text or "per day" in r.text.lower())
        if r.status_code == 404 or daily:
            why = "used up its free daily quota" if daily else "isn't available"
            gemini_exhausted.add(gemini_model)
            old = gemini_model
            if next_gemini_model():
                print(f"Gemini model {old} {why}, switching to {gemini_model}.")
                attempt = 0
                continue
            raise AIQuotaExhausted(f"Gemini model {old} {why} and no other model is left")
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 5:
            attempt += 1
            print(f"Gemini returned {r.status_code}, retrying in {20 * attempt}s.")
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


# Cheap local filter: only comments that could plausibly contain a username go to the AI.
HINT = re.compile(
    r"\b(roblox|rblx|user\s*name|username|user|usr|name|ign|id|add|put|avatar|i'?m|im|me in|waiting|"
    r"join(?:ed)?|feature|include|shout\s*out|pick me|my acc(?:ount)?)\b", re.I)
TOKEN = re.compile(r"(?<![A-Za-z0-9_])@?([A-Za-z0-9_]{3,20})(?![A-Za-z0-9_])")


def maybe_username(text):
    tokens = [t.lstrip("@") for t in TOKEN.findall(text)]
    for t in tokens:
        if UNAME.match(t) and re.search(r"[A-Za-z]", t) and re.search(r"[\d_]", t) and len(t) >= 4:
            return True  # e.g. Cool_Kid, potatos00030
    return bool(HINT.search(text)) and any(len(t) >= 5 for t in tokens)


use_claude = bool(ANTHROPIC_KEY)
ai_exhausted = False


def find_usernames(comments):
    """Returns {comment_id: username or None} for the comments actually checked.
    Comments left out of the result (AI quota ran out) should be retried next run."""
    global use_claude, ai_exhausted
    result = {}
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
            try:
                hits = ask_gemini(batch)
            except AIQuotaExhausted as e:
                ai_exhausted = True
                print(f"Stopping AI checks for today: {e}.")
                return result
        else:
            hits = []
            for i, c in enumerate(batch):
                m = PATTERN.search(c["text"])
                if m:
                    hits.append({"i": i, "username": m.group(1)})
        for c in batch:
            result[c["id"]] = None
        for h in hits:
            try:
                c = batch[int(h.get("i"))]
            except (TypeError, ValueError, IndexError):
                continue
            u = str(h.get("username") or "").lstrip("@").strip()
            if UNAME.match(u) and u.lower() in c["text"].lower():
                result[c["id"]] = u
        print(f"Checked {min(start + BATCH, len(comments))}/{len(comments)} likely comments for usernames.")
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
    likely = [c for c in new if maybe_username(c["text"])]
    for c in new:
        if not maybe_username(c["text"]):
            scanned[c["id"]] = c["published"]   # no possible username, never needs the AI
    print(f"{len(new)} new comments, {len(likely)} might contain a username.")
    total_found = 0
    CHUNK = BATCH * 5  # save progress every 5 batches

    def merge(chunk, found):
        n = 0
        for c in chunk:
            if c["id"] not in found:
                continue                        # not checked yet (quota ran out), retry next run
            scanned[c["id"]] = c["published"]
            u = found[c["id"]]
            if not u:
                continue
            n += 1
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
        return n

    save(SCANNED_FILE, {k: v for k, v in scanned.items() if v > window_start})
    for i in range(0, len(likely), CHUNK):
        chunk = likely[i:i + CHUNK]
        total_found += merge(chunk, find_usernames(chunk))
        # save progress so a failure later doesn't redo this chunk
        save(SCANNED_FILE, {k: v for k, v in scanned.items() if v > window_start})
        save(CANDIDATES_FILE, candidates)
        if ai_exhausted:
            left = sum(1 for c in likely if c["id"] not in scanned)
            print(f"{left} likely comments left to check; the next run continues with them.")
            break
    print(f"{total_found} usernames found this run.")

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
