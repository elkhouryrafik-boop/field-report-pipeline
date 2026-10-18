"""Blur faces in the photographs a run publishes with people in them.

Requirement: publish every photograph, blur the faces of the
people in it — not the picture. This writes blurred copies of the frames listed
in `runs/<slug>/redaction_required.json` to `runs/<slug>/media_redacted/`; the
originals in the export are never touched, and s7 refuses to embed a listed
frame without its copy (REDACTION_MISSING).

Faces come from two sources, unioned:
  1. OpenCV Haar cascades (frontal + profile, both mirror directions);
  2. `runs/<slug>/redaction_boxes.json` — {filename: [[x, y, w, h], ...]} in
     fractions of the image size, for faces a reviewer or a vision check found
     that the detector missed.
Detection is a floor, not a guarantee: every output must be looked at before
it ships. `--report` lists per-file box counts so the check can be targeted.

Usage:
    python -m field_report.tools.redact_faces --config runs/<slug>/config.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


from field_report.lib import common  # noqa: E402

try:
    import cv2
except ImportError:  # pragma: no cover
    print("opencv-python is required: pip install opencv-python", file=sys.stderr)
    sys.exit(2)

PAD = 0.35  # grow each box so hairline, ears and chin are covered too


def detect(img) -> list[tuple[int, int, int, int]]:
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    h, w = gray.shape
    min_side = max(18, min(h, w) // 40)
    boxes = []
    for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + name)
        for flipped in (False, True):
            g = cv2.flip(gray, 1) if flipped else gray
            for (x, y, bw, bh) in casc.detectMultiScale(g, 1.1, 5, minSize=(min_side, min_side)):
                if flipped:
                    x = w - x - bw
                boxes.append((int(x), int(y), int(bw), int(bh)))
    return boxes


def blur_boxes(img, boxes):
    h, w = img.shape[:2]
    for (x, y, bw, bh) in boxes:
        px, py = int(bw * PAD), int(bh * PAD)
        x0, y0 = max(0, x - px), max(0, y - py)
        x1, y1 = min(w, x + bw + px), min(h, y + bh + py)
        roi = img[y0:y1, x0:x1]
        if roi.size == 0:
            continue
        k = max(31, (max(x1 - x0, y1 - y0) // 2) | 1)
        # pixelate then blur: irreversible at report resolution
        small = cv2.resize(roi, (max(1, (x1 - x0) // 12), max(1, (y1 - y0) // 12)))
        roi = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
        img[y0:y1, x0:x1] = cv2.GaussianBlur(roi, (k, k), 0)
    return img


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--no-auto", action="store_true",
                    help="use only redaction_boxes.json. Haar cascades can miss half-turned "
                         "faces; use reviewed boxes only.")
    args = ap.parse_args(argv)
    cfg = common.load_config(args.config)
    run_dir = common.resolve_run_dir(cfg, args.run_dir)

    required = json.loads((run_dir / "redaction_required.json").read_text(encoding="utf-8-sig"))
    manual_path = run_dir / "redaction_boxes.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8-sig")) if manual_path.is_file() else {}
    out_dir = run_dir / "media_redacted"
    out_dir.mkdir(exist_ok=True)

    report, failed = {}, []
    for name in required:
        src = Path(cfg["media_dir"]) / name
        img = cv2.imread(str(src))
        if img is None:
            failed.append(name)
            continue
        h, w = img.shape[:2]
        auto = [] if args.no_auto else detect(img)
        extra = [(int(fx * w), int(fy * h), int(fw * w), int(fh * h))
                 for fx, fy, fw, fh in manual.get(name, [])]
        img = blur_boxes(img, auto + extra)
        cv2.imwrite(str(out_dir / name), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        report[name] = {"auto": len(auto), "manual": len(extra)}

    (run_dir / "redaction_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"redacted {len(report)}/{len(required)}; "
          f"auto boxes on {sum(1 for r in report.values() if r['auto'])}, "
          f"manual on {sum(1 for r in report.values() if r['manual'])}, "
          f"no box at all on {sum(1 for r in report.values() if not (r['auto'] or r['manual']))}")
    if failed:
        print("unreadable: " + ", ".join(failed), file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
