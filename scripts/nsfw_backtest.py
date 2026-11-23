"""Backtest the nudity check against clips Alex already reviewed or that were posted.

Downloads each clip's review preview from R2, runs src/nsfw.py on it, and prints
one line per clip: decision, flag, frame stats. Writes docs/nsfw_backtest.md.
Run by .github/workflows/nsfw-backtest.yml (manual).
"""
import pathlib
import sqlite3
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import common  # noqa: E402
import nsfw  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
conf = common.cfg()
c = sqlite3.connect(ROOT / "state" / "factory.db")
rows = c.execute("""
  SELECT c.clip_id, c.streamer, c.state, c.preview_url,
         (SELECT decision FROM decisions d WHERE d.clip_id=c.clip_id
          ORDER BY decided_at DESC LIMIT 1) dec
  FROM clips c
  WHERE c.preview_url IS NOT NULL AND c.preview_url != ''
    AND (c.state IN ('published','rejected','approved') )
    AND c.first_seen >= datetime('now','-6 days')
  ORDER BY c.first_seen DESC LIMIT ?""", (int(sys.argv[1]) if len(sys.argv) > 1 else 150,)).fetchall()

out = ["| clip | streamer | state | flagged | stats |", "|---|---|---|---|---|"]
n_flag = {"published": 0, "rejected": 0, "approved": 0}
tot = {"published": 0, "rejected": 0, "approved": 0}
tmp = pathlib.Path(tempfile.mkdtemp())
for clip_id, streamer, state, url, dec in rows:
    f = tmp / "c.mp4"
    try:
        urllib.request.urlretrieve(url, f)
        why, stats = nsfw.scan_picture(f, 0, None, conf)
    except Exception as e:  # noqa: BLE001
        print("skip", clip_id, e)
        continue
    tot[state] = tot.get(state, 0) + 1
    if why:
        n_flag[state] = n_flag.get(state, 0) + 1
    line = f"| {clip_id} | {streamer} | {state} | {why or ''} | {stats} |"
    print(line, flush=True)
    out.append(line)

summary = "\n".join(f"- {k}: {n_flag.get(k, 0)} of {v} flagged" for k, v in tot.items())
print(summary)
(ROOT / "docs" / "nsfw_backtest.md").write_text(
    "# Nudity check backtest\n\nPreviews of the last 6 days' clips.\n\n" + summary + "\n\n"
    + "\n".join(out) + "\n")
