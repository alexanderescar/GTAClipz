"""
Perceptual-hash dedupe.

The failure mode this prevents: three streamers clip the same moment, you
render all three, and your feed looks like a bot. Platforms notice.
We hash a few sampled frames and reject anything too close to what we've
already posted.
"""
import subprocess
import tempfile
import pathlib

import imagehash
from PIL import Image

from common import db, log


def frames(path, n=4):
    out = []
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-vf", f"fps=1/{max(1, 3)},scale=256:-1", "-frames:v", str(n),
             f"{td}/f_%02d.png"],
            check=True, capture_output=True,
        )
        for p in sorted(pathlib.Path(td).glob("*.png")):
            out.append(imagehash.phash(Image.open(p)))
    return out


def signature(path):
    return ",".join(str(h) for h in frames(path))


def is_duplicate(sig, threshold=8):
    """True if any stored signature is within `threshold` Hamming distance."""
    conn = db()
    mine = [imagehash.hex_to_hash(h) for h in sig.split(",") if h]
    # Only compare against clips that are (or are about to be) public.
    # Scout proof clips must never block posting that moment once permission
    # arrives, and rejected/expired clips shouldn't block a better cut of it.
    for (other,) in conn.execute(
        "SELECT phash FROM clips WHERE phash IS NOT NULL AND phash != '' "
        "AND lane='publish' AND state IN "
        "('rendered','pending_review','approved','published')"
    ):
        theirs = [imagehash.hex_to_hash(h) for h in other.split(",") if h]
        hits = sum(
            1 for a in mine for b in theirs if (a - b) <= threshold
        )
        if hits >= max(2, len(mine) // 2):
            return True
    return False
