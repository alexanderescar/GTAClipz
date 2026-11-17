"""
One-time YouTube OAuth. Run this ONCE on your own machine (it opens a browser),
then paste the contents of state/youtube_token.json into the GitHub secret
YT_TOKEN_JSON.

Before running:
  1. console.cloud.google.com -> new project
  2. Enable "YouTube Data API v3"
  3. OAuth consent screen -> External -> add yourself as a Test user
  4. Credentials -> OAuth client ID -> Desktop app -> download JSON
  5. Save it as scripts/client_secret.json

Note: a Testing-mode consent screen issues refresh tokens that expire after
7 days. Publish the app (Publishing status -> Publish) to get a long-lived
refresh token. You do NOT need Google's verification review for your own
channel — unverified just means a scary interstitial you click past.
"""
import pathlib
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
SECRET = ROOT / "scripts" / "client_secret.json"
OUT = ROOT / "state" / "youtube_token.json"

if not SECRET.exists():
    sys.exit(f"Put your OAuth desktop-app JSON at {SECRET}")

flow = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
OUT.parent.mkdir(exist_ok=True)
OUT.write_text(creds.to_json())
print(f"\n✓ Wrote {OUT}")
print("\nNow run:  gh secret set YT_TOKEN_JSON < state/youtube_token.json")
if not creds.refresh_token:
    print("\n⚠  No refresh_token returned — delete the token and re-run with "
          "prompt='consent'. Without it the pipeline dies in an hour.")
