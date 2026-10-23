# Kalemod Roblox Picker

Every day at 8:17am (Cebu time) a GitHub Action reads the last 30 days of comments on @kalemod, finds viewers sharing their Roblox username, and picks 20 at random. Nobody is picked twice until you reset.

**Today's list:** [`picks/latest.md`](picks/latest.md). Past days are in [`picks/`](picks/).

## Setup (once)

1. **YouTube API key:** in [Google Cloud Console](https://console.cloud.google.com/), create a project, enable **YouTube Data API v3**, then go to Credentials → Create credentials → API key.
2. **Username detection key (pick one):**
   - **Gemini, free:** at [aistudio.google.com/apikey](https://aistudio.google.com/apikey), click Create API key and choose the same Google Cloud project. Save it as `GEMINI_API_KEY`.
   - **Claude Haiku:** from [console.anthropic.com](https://console.anthropic.com/), saved as `ANTHROPIC_API_KEY`. If both are set, Claude is used.
   - **Neither:** usernames are found by pattern matching, which misses casual ones.
3. In this repo, go to Settings → Secrets and variables → Actions, and add `YT_API_KEY` plus your detection key.
4. In the Actions tab, open **Daily Roblox picks** and click **Run workflow** to get today's list right away.

## Reset picked history

Go to Actions → **Daily Roblox picks** → **Run workflow**, tick **Reset picked history**, and run it. Everyone becomes eligible again, and that run picks the rest of today's 20.

## Cost

- **YouTube:** each run reads about 50 pages, and the free quota allows 10,000 a day.
- **Detection:** only comments it hasn't checked before are sent. Gemini's free tier covers this, and Claude Haiku would cost a few cents a month.

## Files

- `data/candidates.json`: every username found, with when it was last mentioned and when it was picked.
- `data/scanned_comments.json`: comments already checked, so they aren't sent to Claude again.
- `data/days/`: each day's picks as data.
