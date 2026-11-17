# Setup — credential by credential

Budget ~90 minutes. Do YouTube and Instagram first; they're the ones that
fully automate. Start the TikTok audit application on day one so it clears
while you're busy.

---

## 1. Twitch (discovery) — 5 min

1. dev.twitch.tv/console/apps → **Register Your Application**
2. OAuth Redirect URL: `http://localhost` · Category: Application Integration
3. Copy **Client ID**, generate a **Client Secret**

```bash
gh secret set TWITCH_CLIENT_ID
gh secret set TWITCH_CLIENT_SECRET
```

No review, no approval. Client-credentials flow — no user login needed.

---

## 2. YouTube (publishing) — 25 min

1. console.cloud.google.com → new project
2. **APIs & Services → Library** → enable *YouTube Data API v3*
3. **OAuth consent screen** → External → add your own Google account as a Test user
4. **Credentials → Create → OAuth client ID → Desktop app** → download JSON
5. Save as `scripts/client_secret.json`

```bash
python scripts/auth_youtube.py
gh secret set YT_TOKEN_JSON < state/youtube_token.json
```

**Two traps:**
- A consent screen left in *Testing* issues refresh tokens that die after
  **7 days**. Hit **Publish app**. You don't need Google's verification
  review to post to your own channel — unverified just shows an
  "unverified app" interstitial you click through once.
- If `auth_youtube.py` reports no `refresh_token`, delete the token file and
  re-run. Without it the pipeline stops working within the hour.

**Quota:** `videos.insert` has its own bucket — 100 uploads/day per project.
You'll hit YouTube's per-channel upload limit first.

---

## 3. Instagram (publishing) — 30 min

1. Switch your IG account to a **Creator** account and link it to a Facebook
   Page. (Creator now supports automated Reels posting, and unlike Business it
   gets Instagram's monetization — gifts/stars, subscriptions — and the full
   music library.)
2. developers.facebook.com → **Create App** → *Business* type
3. Add the **Instagram** product → **Instagram API with Instagram Login**
4. **App roles → add your own IG account as an Instagram Tester**, then accept
   the invite from your Instagram app settings
5. Generate a token with `instagram_business_content_publish` +
   `instagram_business_basic`
6. Get your IG user id from the Graph API Explorer (`/me?fields=id`)

```bash
gh secret set IG_USER_ID
gh secret set IG_ACCESS_TOKEN
```

**You do NOT need App Review.** Development mode covers accounts you own.
App Review / Advanced Access is only for publishing to *other people's*
accounts.

**Token lifetime:** long-lived tokens last 60 days and must be refreshed.
Put a calendar reminder at day 50 — this is the #1 cause of a pipeline that
silently stops posting.

---

## 4. Cloudflare R2 (Instagram needs a public URL) — 15 min

Instagram's API can't take a file upload. It fetches from a public
`video_url`. R2 is free and has zero egress fees.

1. dash.cloudflare.com → **R2** → Create bucket `gta6-clips`
2. **Settings → Public access** → enable, or attach a custom domain
3. **Manage API Tokens** → Create → Object Read & Write
4. Put the public base URL in `config/settings.yml` → `storage.r2_public_base`

```bash
gh secret set R2_ACCOUNT_ID
gh secret set R2_ACCESS_KEY_ID
gh secret set R2_SECRET_ACCESS_KEY
```

---

## 5. TikTok — 15 min now, 1–3 weeks to clear

1. developers.tiktok.com → Create app
2. Add **Content Posting API**
3. Request scopes `video.upload` (inbox drafts) and `video.publish` (direct)
4. **Products → Content Posting API → Direct Post → apply for the audit**

```bash
gh secret set TIKTOK_ACCESS_TOKEN
```

Leave `publish.tiktok.mode: inbox_draft` in settings until the audit clears.
Unaudited direct posts return HTTP 200 and publish something **only you can
see** — the single most wasted week in short-form automation.

---

## 6. Go

```bash
gh repo create gta6-clip-factory --private --source=. --push
gh workflow enable "GTA6 Clip Factory"
gh workflow run "GTA6 Clip Factory" -f mode=status
```

Watch the first live run before launch night:
```bash
gh run watch
```

---

## Health checks

```bash
python src/pipeline.py status     # queue + posts today
python src/outreach.py            # who to ask next
```

| Symptom | Cause |
|---|---|
| YouTube uploads stop after ~1h | No refresh token — re-run auth with `prompt=consent` |
| YouTube uploads stop after 7 days | Consent screen still in Testing — publish the app |
| Instagram stops after 60 days | Access token expired — refresh it |
| TikTok posts nobody can see | Direct Post audit not passed — switch to `inbox_draft` |
| IG container stuck at `IN_PROGRESS` | `r2_public_base` not actually public — open the URL in a private window |
| Everything `blocked_no_permission` | Working as designed. Populate `sources.yml`. |
