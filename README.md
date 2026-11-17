# GTA 6 Clip Factory

Automated clip → vertical short → YouTube Shorts + TikTok + Instagram Reels.
Zero paid services. Runs on GitHub Actions free tier.

```
 Twitch clips ─┐
               ├→ render → dedupe → REVIEW → publish
 Long VODs ────┘   9:16     phash   (you,     YouTube
 chat/audio        hook             then      Instagram
 spikes            captions         learned)  TikTok
```

## Two lanes

| Lane | Sources | What happens |
|---|---|---|
| **publish** | campaigns, streamers who said yes, your capture | rendered → your approval → posted |
| **scout** | everyone else, incl. auto-discovered top GTA 6 streams | 2 proof clips per streamer per day, uploaded for your outreach DMs. **Never posted.** |

## Long-form clipper

Finds moments in multi-hour VODs without downloading them:

1. **Chat spikes** — messages per 10s vs the trailing 5-min median; hype tokens
   (KEKW, LMAO, W, ???, 💀) count extra. Window starts 30s before the spike,
   because chat reacts after the moment.
2. **Audio peaks** — fallback when there's no chat replay (Kick, edited videos),
   and a score boost when a chat spike also has a loudness spike.
3. **Window-only downloads** — `yt-dlp --download-sections` fetches just the
   45-second pieces. That's what keeps an 8-hour VOD inside a free runner.
4. **Growing VODs** (marathons) are scanned incrementally, 5 min behind live.

Add any long video by pasting it into `config/vod_queue.yml`.

## Approval queue

Week one: each run adds new clips to a GitHub Issue titled *Clip review — <date>*,
each with a preview link and two checkboxes. Tick ✅ or ❌ in the GitHub phone
app; the next run acts on it. Unanswered clips expire after 36 hours.

From `review.auto_switch_after` (and ≥40 decisions) it switches itself to
auto: per clip type, it learns the lowest score at which you approved ≥80%
of clips, and applies that. `python src/review.py report` shows what it learned.

## The one rule

`config/sources.yml` gives every source a `permission` field. The pipeline
**refuses to publish** anything that isn't `campaign`, `granted`, `published`,
or `own`. This is load-bearing, not decorative:

- YouTube's **reused-content policy is channel-wide and applies even when you
  have permission** — a channel of bare reuploads can't enter the Partner
  Program at all. Transformation (hook, captions, your voice) is what makes it
  monetizable; permission is what keeps it alive.
- Three DMCA strikes deletes a YouTube channel.
- Instagram's cross-platform content matching (Dec 2025) detects originals and
  suppresses copies.

Unpermissioned streamers aren't discarded — they become your outreach queue:
`python src/outreach.py`.

## Quick start

```bash
pip install -r requirements.txt
sudo apt install ffmpeg fonts-dejavu-core       # or: brew install ffmpeg

python scripts/auth_youtube.py                   # once, locally
python src/pipeline.py status
python src/pipeline.py run
```

Full credential walkthrough: [docs/SETUP.md](docs/SETUP.md)

## Commands

| Command | Cost | Cadence |
|---|---|---|
| `pipeline.py discover` | seconds | every 30 min |
| `pipeline.py download` | ~10s/clip | follows discover |
| `pipeline.py render` | ~45s/clip | hourly |
| `pipeline.py publish` | ~90s/clip | respects daily caps |
| `pipeline.py run` | all of it | what Actions runs |
| `pipeline.py status` | — | anytime |
| `pipeline.py vods` / `longform` | medium | every run |
| `pipeline.py review` | seconds | every run |
| `outreach.py` | — | daily: who to ask + proof clips to attach |
| `review.py report` | — | your approval stats + learned thresholds |

## Platform reality check

| | Automated? | Gate |
|---|---|---|
| **YouTube Shorts** | ✅ fully | None. Google reversed the private-upload rule on **2026-11-02** — unverified projects now publish public. 100 uploads/day. |
| **Instagram Reels** | ✅ fully | Business account + dev-mode app. **No App Review needed for your own account.** Needs a public `video_url` → R2. |
| **TikTok** | ⚠️ semi | Direct Post needs a separate audit (1–3 weeks). Until then clips land in your inbox as drafts — you tap post. |

## Cost

| | Free tier | Headroom |
|---|---|---|
| GitHub Actions | unlimited (public repo) / 2,000 min mo | ~250 cycles/mo private |
| Cloudflare R2 | 10 GB, zero egress | ~1,600 clips |
| Twitch / YouTube / IG / TikTok APIs | free | — |
| faster-whisper, ffmpeg, yt-dlp | free, local | — |

**Total: $0/month.**
