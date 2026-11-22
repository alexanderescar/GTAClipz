"""Find the streamer's webcam in a clip, so the split layout always shows a face.

Why: streamers move or hide their cam (Clix, Nov 21: a different overlay, so the
fixed crop showed dark gameplay instead of his face). Alex rejects every clip
where the streamer isn't visible, so:

  1. Check the cam box configured in sources.yml (facecam:). If a face shows up
     there in enough sampled frames, use it.
  2. Otherwise look for a face that stays in the same spot across the clip
     (a webcam doesn't move; in-game characters do) and build the cam box
     around it.
  3. Otherwise report no face. The caller drops the clip instead of sending
     Alex something he'll reject.

Face detector: OpenCV's YuNet (models/face_detection_yunet_2023mar.onnx, MIT).
"""
import pathlib
import subprocess

from common import log

MODEL = pathlib.Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLES = 6


def _frames(src, start, duration, n=SAMPLES):
    import cv2
    import numpy as np
    out = []
    for i in range(n):
        t = start + duration * (i + 0.5) / n
        png = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
             "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=60).stdout
        if png:
            img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                out.append(img)
    return out


def _detect(img, min_score=0.6):
    import cv2
    h, w = img.shape[:2]
    det = cv2.FaceDetectorYN.create(str(MODEL), "", (w, h), min_score, 0.3, 50)
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    return [] if faces is None else [tuple(map(float, f[:4])) + (float(f[-1]),) for f in faces]


def _faces_in_box(img, box):
    """Faces inside a fractional box, upscaled 2x so small cams still register."""
    import cv2
    h, w = img.shape[:2]
    fx, fy, fw, fh = box
    x0, y0 = int(fx * w), int(fy * h)
    crop = img[y0:y0 + int(fh * h), x0:x0 + int(fw * w)]
    if crop.size == 0:
        return []
    crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return _detect(crop, 0.55)


def find_cam(src, facecam, start, duration, need=2):
    """Return (box, why). box is a fractional [x, y, w, h] cam box or None."""
    if not MODEL.exists():
        log.warning("faces: model missing, using configured facecam as-is")
        return facecam, "no model"
    try:
        frames = _frames(src, start, duration)
    except Exception as e:  # noqa: BLE001
        return facecam, f"frame grab failed: {e}"
    if not frames:
        return facecam, "no frames"

    # 1. the configured cam box
    if facecam:
        hits = sum(1 for f in frames if _faces_in_box(f, facecam))
        if hits >= need:
            return facecam, f"face in configured cam {hits}/{len(frames)}"

    # 2. a face that stays put
    h, w = frames[0].shape[:2]
    spots = []
    for f in frames:
        for x, y, fw, fh, s in _detect(f):
            if fw < 0.025 * w:                   # tiny = background/in-game
                continue
            spots.append((x + fw / 2, y + fh / 2, fw, fh))
    best, best_n = None, 0
    for cx, cy, fw, fh in spots:
        same = [s for s in spots if abs(s[0] - cx) < 0.04 * w and abs(s[1] - cy) < 0.06 * h]
        if len(same) > best_n:
            best, best_n = same, len(same)
    if best and best_n >= max(need + 1, len(frames) // 2):
        cx = sorted(s[0] for s in best)[len(best) // 2]
        cy = sorted(s[1] for s in best)[len(best) // 2]
        fw = sorted(s[2] for s in best)[len(best) // 2]
        # a typical cam: about 4.5 face-widths wide, 16:9, face a little below centre (cams end at the shoulders)
        bw = min(w, fw * 4.5)
        bh = min(h, bw * 9 / 16)
        x0 = min(max(0, cx - bw / 2), w - bw)
        y0 = min(max(0, cy - bh * 0.55), h - bh)
        box = [round(x0 / w, 3), round(y0 / h, 3), round(bw / w, 3), round(bh / h, 3)]
        return box, f"face found at {box} in {best_n}/{len(frames)} frames"

    return None, "no streamer face in this clip"
