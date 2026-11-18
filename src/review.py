"""
Approval queue + learning loop.

WEEK ONE (review.mode: manual)
  Every rendered publish-lane clip gets uploaded to R2 for a preview link and
  added to today's GitHub Issue as two checkboxes:
      - [ ] ✅ Approve `<clip_id>`
      - [ ] ❌ Reject  `<clip_id>`
  You tick boxes from the GitHub app on your phone. The next run reads the
  issue, posts approved clips, drops rejected ones, and logs every decision
  with the clip's features (score, chat spike size, audio lift, length,
  streamer). Unanswered clips expire after `expire_hours`.

AFTER (review.mode: auto, or auto_switch_after reached with enough decisions)
  `learn()` finds, for each kind of clip, the lowest score at which you
  approved at least `target_approval_rate` of clips. Clips at or above it are
  auto-approved; below it, auto-rejected. Every auto decision is still logged,
  and `python src/review.py report` shows how its choices compare to yours.

Why a GitHub Issue: it's free, it's already where the code runs, it has a
phone app with notifications, and checkboxes in an issue are tappable.
"""
import datetime as dt
import json
import os
import re
import sys

import requests

from common import ROOT, cfg, db, log

API = "https://api.github.com"
MODEL = ROOT / "state" / "review_model.json"
BOX = re.compile(r"- \[([ xX])\] (✅ Approve|❌ Reject) `([^`]+)`")


# ── GitHub plumbing ──────────────────────────────────────────────────────────
def _gh(method, path, **kw):
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (tok and repo):
        raise RuntimeError("GITHUB_TOKEN / GITHUB_REPOSITORY unset (set in Actions)")
    r = requests.request(method, f"{API}/repos/{repo}{path}", timeout=30,
                         headers={"Authorization": f"Bearer {tok}",
                                  "Accept": "application/vnd.github+json"}, **kw)
    r.raise_for_status()
    return r.json() if r.text else {}


def todays_issue(label):
    title = f"Clip review — {dt.date.today().isoformat()}"
    for iss in _gh("GET", "/issues", params={"labels": label, "state": "open",
                                             "per_page": 30}):
        if iss["title"] == title:
            return iss
    owner = os.environ.get("GITHUB_REPOSITORY", "/").split("/")[0]
    return _gh("POST", "/issues", json={
        "title": title, "labels": [label], "assignees": [owner],
        "body": ("Tick ✅ to post a clip, ❌ to drop it. Untouched clips expire.\n"
                 "Your ticks are picked up within ~3 minutes. Campaign clips "
                 "post during your campaign hours (settings.yml) so you can "
                 "submit the link in time.\n\n")})


# ── rendering a clip as markdown ─────────────────────────────────────────────
def clip_block(c):
    bits = [f"score **{c['score']:.0f}**", f"{c['duration'] or 0:.0f}s"]
    if c["chat_ratio"]:
        bits.append(f"chat ×{c['chat_ratio']:.1f}")
    if c["audio_lift"]:
        bits.append(f"audio +{c['audio_lift']:.0f} dB")
    prev = (f"[Watch / download for TikTok]({c['preview_url']})"
            if c["preview_url"] else "_preview unavailable — set storage.r2_public_base_")
    chat = f" · chat said: **{c['chat_top']}**" if c["chat_top"] else ""
    return (f"\n---\n### {c['streamer']} — {c['note'] or ''}\n"
            f"{' · '.join(bits)}{chat}\n\n▶️ {prev}\n\n"
            f"- [ ] ✅ Approve `{c['clip_id']}`\n"
            f"- [ ] ❌ Reject `{c['clip_id']}`\n")


def parse_decisions(body):
    """{clip_id: 'approved'|'rejected'} for every ticked box. Reject wins ties."""
    out = {}
    for ticked, kind, cid in BOX.findall(body or ""):
        if ticked.strip():
            d = "rejected" if "Reject" in kind else "approved"
            if out.get(cid) != "rejected":
                out[cid] = d
    return out


# ── decisions + learning ─────────────────────────────────────────────────────
def record(conn, c, decision):
    conn.execute(
        """INSERT OR REPLACE INTO decisions (clip_id, decision, source_kind, score,
           chat_ratio, audio_lift, duration, streamer, hook)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (c["clip_id"], decision, c["source_kind"], c["score"], c["chat_ratio"],
         c["audio_lift"], c["duration"], c["streamer"], c["note"]))
    state = "approved" if decision in ("approved", "auto_approved") else "rejected" \
        if decision != "expired" else "expired"
    conn.execute("UPDATE clips SET state=? WHERE clip_id=?", (state, c["clip_id"]))


def learn(target=0.70, min_n=8):
    """Per clip kind: lowest score whose approval rate (at and above it) >= target."""
    conn = db()
    model = {}
    kinds = [k for (k,) in conn.execute(
        "SELECT DISTINCT source_kind FROM decisions WHERE decision IN "
        "('approved','rejected')")]
    for k in kinds:
        rows = conn.execute(
            "SELECT score, decision FROM decisions WHERE source_kind=? AND "
            "decision IN ('approved','rejected') ORDER BY score DESC", (k,)).fetchall()
        if len(rows) < min_n:
            continue
        best, ok = None, 0
        for i, (score, d) in enumerate(rows, 1):
            ok += d == "approved"
            if i >= min_n // 2 and ok / i >= target:
                best = score        # still above target this far down: keep going
        model[k] = {"threshold": best, "n": len(rows),
                    "approval_rate": round(sum(d == "approved" for _, d in rows)
                                           / len(rows), 2)}
    MODEL.write_text(json.dumps(model, indent=2))
    return model


def is_auto(conf, conn):
    r = conf["review"]
    if r["mode"] == "auto":
        return True
    n = conn.execute("SELECT COUNT(*) FROM decisions WHERE decision IN "
                     "('approved','rejected')").fetchone()[0]
    return (dt.date.today().isoformat() >= r["auto_switch_after"]
            and n >= r["min_decisions"])


# ── the stage ────────────────────────────────────────────────────────────────
def stage(conn, conf):
    conn.row_factory = __import__("sqlite3").Row
    r = conf["review"]

    if is_auto(conf, conn):
        model = learn(r["target_approval_rate"])
        for c in conn.execute("SELECT * FROM clips WHERE state='rendered' "
                              "AND lane='publish'").fetchall():
            t = (model.get(c["source_kind"]) or {}).get("threshold")
            if t is None:       # never reviewed this kind: ask a human after all
                continue
            record(conn, c, "auto_approved" if c["score"] >= t else "auto_rejected")
        conn.commit()
        log.info("review: auto mode, model=%s", model)
        if not conn.execute("SELECT 1 FROM clips WHERE state='rendered' AND "
                            "lane='publish'").fetchone():
            return

    # 1. read decisions from open review issues
    try:
        issues = _gh("GET", "/issues", params={"labels": r["issue_label"],
                                               "state": "open", "per_page": 30})
    except RuntimeError as e:
        log.warning("review queue offline: %s", e)
        return
    for iss in issues:
        for cid, d in parse_decisions(iss.get("body")).items():
            c = conn.execute("SELECT * FROM clips WHERE clip_id=? AND "
                             "state='pending_review'", (cid,)).fetchone()
            if c:
                record(conn, c, d)
                log.info("review: %s -> %s", cid, d)
    conn.commit()

    # 2. expire stale ones
    cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=r["expire_hours"])
              ).isoformat(sep=" ")
    for c in conn.execute("SELECT * FROM clips WHERE state='pending_review' AND "
                          "queued_at < ?", (cutoff,)).fetchall():
        record(conn, c, "expired")
    conn.commit()

    # 3. queue newly rendered clips
    new = conn.execute("SELECT * FROM clips WHERE state='rendered' AND "
                       "lane='publish' ORDER BY score DESC").fetchall()
    if not new:
        return
    iss = todays_issue(r["issue_label"])
    body = iss.get("body") or ""
    for c in new:
        if f"`{c['clip_id']}`" not in body:
            body += clip_block(c)
        conn.execute("UPDATE clips SET state='pending_review', review_issue=?, "
                     "queued_at=datetime('now') WHERE clip_id=?",
                     (iss["number"], c["clip_id"]))
    _gh("PATCH", f"/issues/{iss['number']}", json={"body": body})
    conn.commit()
    log.info("review: queued %d clips in issue #%s", len(new), iss["number"])


def report():
    conn = db()
    print("\n  decisions so far")
    for d, n in conn.execute("SELECT decision, COUNT(*) FROM decisions GROUP BY 1"):
        print(f"    {d:<15} {n:>4}")
    print("\n  learned thresholds:")
    print(json.dumps(learn(cfg()["review"]["target_approval_rate"]), indent=4))
    print("\n  your approval rate by streamer:")
    for s, a, n in conn.execute(
            "SELECT streamer, SUM(decision='approved'), COUNT(*) FROM decisions "
            "WHERE decision IN ('approved','rejected') GROUP BY streamer "
            "ORDER BY 3 DESC"):
        print(f"    {s:<24} {a}/{n}")
    print()


if __name__ == "__main__":
    report() if (sys.argv[1:] or ["report"])[0] == "report" else None
