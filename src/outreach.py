"""
Turn discovered-but-unpermissioned streamers into an outreach queue.

The pipeline refuses to publish `permission: none`. That's not a dead end —
it's a lead list. This prints who is producing clippable GTA 6 moments right
now, ranked by how much traction their clips are getting, so you ask the
right people first.

    python src/outreach.py            # show the queue
    python src/outreach.py mark NAME contacted
"""
import sys

from common import db, log


def queue(limit=30):
    conn = db()
    rows = conn.execute(
        """
        SELECT streamer, platform, COUNT(*) AS clips, SUM(views) AS total_views,
               ROUND(AVG(score),1) AS avg_score
        FROM clips
        WHERE state IN ('blocked_no_permission','discovered')
        GROUP BY streamer
        ORDER BY total_views DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    status = dict(conn.execute("SELECT streamer, status FROM outreach"))
    print(f"\n  {'streamer':<26}{'plat':<9}{'clips':>6}{'views':>10}"
          f"{'score':>8}  status")
    print("  " + "-" * 74)
    for s, plat, n, views, score in rows:
        print(f"  {(s or '?')[:25]:<26}{plat:<9}{n:>6}{views or 0:>10,}"
              f"{score:>8}  {status.get(s,'— not contacted')}")
    proofs = conn.execute(
        "SELECT streamer, preview_url, chat_top FROM clips WHERE state='scouted' "
        "ORDER BY score DESC").fetchall()
    if proofs:
        print("\n  proof-of-concept clips to attach to your DMs")
        print("  " + "-" * 74)
        for s, url, top in proofs:
            print(f"  {(s or '?')[:24]:<26}{url or '(no R2 preview — see work/out/)'}"
                  f"{'   chat: ' + top if top else ''}")
    print(f"\n  {len(rows)} streamers. Ask the top ones first; they produce the"
          f"\n  clips worth rendering. Template: docs/outreach-template.md\n")


def mark(streamer, state, channel="dm", note=""):
    conn = db()
    conn.execute(
        """INSERT INTO outreach (streamer, contacted_at, channel, status, note)
           VALUES (?, datetime('now'), ?, ?, ?)
           ON CONFLICT(streamer) DO UPDATE SET status=excluded.status,
               contacted_at=excluded.contacted_at, note=excluded.note""",
        (streamer, channel, state, note),
    )
    conn.commit()
    log.info("%s -> %s", streamer, state)
    if state == "granted":
        log.info("Now add them to config/sources.yml with permission: granted "
                 "and save the evidence in docs/permissions/")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "mark":
        mark(sys.argv[2], sys.argv[3], *(sys.argv[4:]))
    else:
        queue()
