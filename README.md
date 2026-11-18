# ▶ CLIPFARM — GTA6 auto-clipping farm

Watches the YouTube creators you choose, finds the best moments in their new videos,
turns them into vertical Shorts with captions, and posts a few a day to your channel.

```
 ┌──────────── GitHub Actions (hourly) ────────────┐        ┌──────── Netlify ────────┐
 │ tick (≈20s, no installs)                         │        │ Dashboard               │
 │   discover → RSS feeds of your creators          │        │  • add creators         │
 │   publish  → posts best clip when a slot is due  │◀──────▶│  • preview / approve    │
 │   cleanup  → frees storage                       │ Supabase │  • download (manual)   │
 │ process (only when there's work)                 │  (DB +   │  • settings / schedule │
 │   yt-dlp → find moments → download just those    │ storage) │  • "Run now" button    │
 │   seconds → ffmpeg 1080×1920 + captions → store  │        └─────────────────────────┘
 └──────────────────────────────────────────────────┘
```

**How it picks moments** (best signal available wins):
1. **Most-replayed heatmap** — the spikes on YouTube's own replay graph (what viewers rewatched)
2. **Captions** — reactions ("no way", "oh my god", laughter, "clip that"), GTA words (chase, five stars, wasted, glitch), fast excited speech
3. **Audio** — loudness spikes relative to the surrounding minute
4. **Evenly spaced** — only if nothing stands out at all

Cuts are nudged into pauses between words so clips don't start mid-sentence. Each Short gets
big pop-in captions, a hook title on top, a "via Creator" credit, and a description linking the
original video at the exact timestamp.

---

## ⚠️ Read this first (it decides whether your channel survives)

- **Only clip creators who've given you permission.** Re-uploading someone's video without permission
  gets copyright strikes, and three strikes deletes a channel. Many big creators *want* clippers and run
  clipping programs — ask, and note where they said yes. The farm only touches creators with
  **Permission** ticked in the dashboard.
- **YouTube API audit.** Until your Google Cloud project passes YouTube's API compliance audit,
  every video uploaded through the API is **locked to private, permanently** (it can't be un-privated later).
  That's why the farm starts in **manual mode**: it renders clips and you post them from the YouTube app
  in a few taps. Apply for the audit on day one (step 3), then flip **Settings → Upload mode → api**.
- **Monetisation.** YouTube's "reused content" policy demonetises channels that just re-post other people's
  videos. Captions + hooks help; your own commentary, edits and compilations help much more.

---

## Setup (≈45 minutes, all free tiers)

### 1 · Supabase (database + clip storage)
1. Create a project at [supabase.com](https://supabase.com).
2. Open `supabase/schema.sql`, change `you@example.com` to **your** email, then paste the whole file into
   **SQL Editor → Run**.
3. Copy three values (gear icon **Project Settings**, bottom of the left sidebar):
   - **Project URL** → Project Settings → **Data API** (looks like `https://abcd1234.supabase.co`)
   - **Publishable key** (`sb_publishable_…`) → Project Settings → **API Keys**
   - **Secret key** (`sb_secret_…`) → same page; click the eye/copy icon to reveal it
   If you only see a **Create new API keys** button, click it. (The older `anon` / `service_role` keys on the
   *Legacy API Keys* tab also work, but Supabase is retiring them, so use the new ones.)
4. After step 5 (Netlify), come back to **Authentication → URL Configuration** and set **Site URL** to your Netlify URL.

### 2 · GitHub (the worker)
1. Create a new repo (public is simplest: standard Actions runners are free for public repos, and secrets
   stay hidden. A private repo works too but uses your plan's monthly Actions minutes).
2. Push this folder to it.
3. **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Value |
|---|---|---|
| `SUPABASE_URL` | ✅ | Project URL from step 1 |
| `SUPABASE_SERVICE_ROLE_KEY` | ✅ | Secret key `sb_secret_…` (keep it secret!) |
| `YT_CLIENT_ID` | ✅* | from step 3 |
| `YT_CLIENT_SECRET` | ✅* | from step 3 |
| `YT_REFRESH_TOKEN` | ✅* | from step 3 |
| `YTDLP_COOKIES` | recommended | see *Bot check* below |
| `ANTHROPIC_API_KEY` | optional | AI-written hook titles (otherwise templates) |
| `YTDLP_PROXY` | optional | e.g. a residential proxy URL |

\* Needed to resolve `@handles` and to auto-post. In manual mode with `UC…` channel IDs, the farm runs without them.

### 3 · Google Cloud (YouTube access)
1. [console.cloud.google.com](https://console.cloud.google.com) → new project → **APIs & Services → Library** → enable **YouTube Data API v3**.
2. **OAuth consent screen** → External → fill the basics → add yourself as a test user.
   Then click **Publish app → In production**. *(If you leave it in "Testing", your refresh token dies after 7 days.)*
   You'll see an "unverified app" warning when you sign in — that's fine for your own channel.
3. **Credentials → Create credentials → OAuth client ID → Desktop app.** Copy the client ID + secret.
4. On your own computer:
   ```bash
   python3 scripts/get_refresh_token.py YOUR_CLIENT_ID YOUR_CLIENT_SECRET
   ```
   Sign in, **pick your clip channel**, and paste the printed token into the `YT_REFRESH_TOKEN` secret.
5. **Apply for the API audit** (this is what unlocks public auto-posting):
   <https://support.google.com/youtube/contact/yt_api_form>. Describe it honestly: a personal tool that
   uploads Shorts to your own channel from creators who've authorised clipping.

### 4 · Netlify (the dashboard)
1. [app.netlify.com](https://app.netlify.com) → **Add new site → Import from Git** → pick the repo. Build settings come from `netlify.toml`.
2. **Site configuration → Environment variables**:

| Variable | Value |
|---|---|
| `SUPABASE_URL` | Project URL |
| `SUPABASE_ANON_KEY` | Publishable key `sb_publishable_…` (public-safe; RLS protects data) |
| `GITHUB_REPO` | `your-username/your-repo` |
| `GITHUB_TOKEN` | Fine-grained token → this repo only → **Actions: Read and write** |
| `GITHUB_BRANCH` | `main` (optional) |

3. Deploy, then set the Supabase **Site URL** (step 1.4) to the Netlify URL.

### 5 · First run
1. Open the dashboard → enter your email → click the magic link.
2. **Creators** → add `@handle`s, tick **I have permission**, add a note.
3. **Overview → ⚡ Run farm now.** Watch it in GitHub → **Actions**. The first run resolves handles and finds
   videos; the clipping job starts right after if there's work.
4. **Queue** → preview clips, edit titles, then **⬇ Download** and post from the YouTube app (manual mode),
   then **Mark posted**. Once your audit is approved: **Settings → Upload mode → api**, and the farm posts at
   your **Post times** automatically.

---

## 🤖 Bot check ("Sign in to confirm you're not a bot")

YouTube often blocks downloads from cloud servers like GitHub's. Two fixes; the second is the strongest:

**A. Cookies.** In a *spare* Google account (not your channel's account), sign in to YouTube in a private
window, export cookies with a "cookies.txt" browser extension (Netscape format), close the window,
and paste the file's whole contents into the `YTDLP_COOKIES` secret. Refresh them if errors come back.

**B. Run clipping on your own computer.** GitHub → repo **Settings → Actions → Runners → New self-hosted runner**,
follow the install steps on a PC that's usually on, install `ffmpeg`, then add the repo **variable**
`PROCESS_RUNNER` = `self-hosted`. The light hourly job stays on GitHub; only the heavy clipping runs at home,
on your home internet connection.

---

## Tuning (Dashboard → Settings)

| Setting | Default | What it does |
|---|---|---|
| Post times | 09:00, 13:00, 18:00 | When clips go out (your timezone). Late cron runs catch up within 3 h. |
| Clips per post time | 1 | 3 times × 1 = 3 Shorts/day |
| Require approval | off | On = every clip waits for you in Queue |
| Clips per source video | 3 | Per creator override in Creators tab |
| Target / min / max length | 40 / 20 / 58 s | |
| Only clip videos newer than | 72 h | Fresh content performs best |
| Max ready backlog | 12 | Stops rendering when the queue is full, so storage and runner minutes aren't wasted |
| Storage budget | 800 MB | Supabase free storage is 1 GB; clips are ~10–30 MB |
| Title template | `{hook} #GTA6 #Shorts` | `{hook}` is AI-written if `ANTHROPIC_API_KEY` is set |

The hourly schedule lives in `.github/workflows/farm.yml` (`cron: "7 * * * *"`).

## Running locally
```bash
export SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=...   # + YT_* for posting
pip install "yt-dlp[default]"                          # plus ffmpeg + deno on your PATH
python -m worker.tick       # discover + publish + cleanup
python -m worker.process    # clip pending videos
FORCE_PUBLISH=1 python -m worker.tick   # post one clip now
python -m unittest discover -s tests -t . -v
```

## Project layout
```
supabase/schema.sql          tables, security policies, storage bucket
worker/tick.py               hourly: discover, schedule + publish, cleanup
worker/process.py            clip pipeline
worker/highlights.py         moment detection (heatmap / captions / audio)
worker/media.py              yt-dlp + ffmpeg (section downloads, 9:16 render, ASS captions)
worker/youtube.py            OAuth refresh, RSS, handle lookup, resumable upload
worker/titles.py             hooks, templates, hashtags (+ optional Claude titles)
.github/workflows/farm.yml   the hourly schedule
dashboard/public/            Netlify dashboard (static, Supabase JS)
netlify/functions/trigger.mjs  "Run now" → GitHub workflow dispatch
scripts/get_refresh_token.py one-time YouTube auth helper
```
