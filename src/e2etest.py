"""
End-to-end smoke test with a generated test pattern (no real footage).

  render -> R2 preview -> YouTube (PRIVATE) -> TikTok (inbox DRAFT)
  -> a sample review issue so you can see what the queue looks like.

Instagram is skipped: it has no private posting, so a test there would be
public. Nothing here touches the clip database, so ticking the sample
issue's boxes does nothing.
"""
import copy
import subprocess

from common import WORK, cfg, ensure_dirs, log


def run():
    ensure_dirs()
    conf = copy.deepcopy(cfg())
    src = WORK / "raw" / "e2e_test_src.mp4"
    out = WORK / "out" / "e2e_test.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=12",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
        check=True)
    results = {}

    import transform
    transform.render(src, out, hook_text="Pipeline test - please ignore",
                     conf=conf, duration=12)
    info = transform.probe(out)
    results["render"] = f"OK {info['width']}x{info['height']}, {info['duration']:.1f}s"

    preview = None
    try:
        import r2
        st = conf["storage"]
        preview = r2.put_public(out, "review/e2e_test.mp4", st["r2_bucket"],
                                st["r2_public_base"])
        results["r2"] = f"OK {preview}"
    except Exception as e:  # noqa: BLE001
        results["r2"] = f"FAILED {e}"

    try:
        import publish_youtube
        conf["publish"]["youtube"]["privacy"] = "private"
        r = publish_youtube.upload(
            out, "GTAClipz pipeline test (private)",
            "Automated pipeline test. Private. Safe to delete.",
            ["test"], conf)
        results["youtube"] = f"OK (PRIVATE) {r['url']}"
    except Exception as e:  # noqa: BLE001
        results["youtube"] = f"FAILED {e}"

    try:
        import publish_tiktok
        import tiktok_auth
        r = publish_tiktok.upload(out, "GTAClipz pipeline test - delete me",
                                  tiktok_auth.access_token(), mode="inbox_draft")
        results["tiktok"] = f"OK status={r.get('status')} (check TikTok inbox/drafts)"
    except Exception as e:  # noqa: BLE001
        results["tiktok"] = f"FAILED {e}"

    try:
        import review
        block = review.clip_block({
            "clip_id": "e2e-test", "streamer": "TEST PATTERN",
            "note": "Pipeline test - please ignore", "score": 123,
            "duration": 12, "chat_ratio": 6.2, "audio_lift": 12,
            "chat_top": "NO WAY x212", "preview_url": preview})
        iss = review._gh("POST", "/issues", json={
            "title": "Clip review — SAMPLE (pipeline test)",
            "body": ("This is what each day's review page looks like. "
                     "Ticking boxes here does nothing. Close this issue when done.\n" + block)})
        results["review"] = f"OK issue #{iss['number']} {iss['html_url']}"
    except Exception as e:  # noqa: BLE001
        results["review"] = f"FAILED {e}"

    print("\n  END-TO-END TEST")
    for k, v in results.items():
        print(f"  {k:<8} {v}")
