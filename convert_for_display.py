#!/usr/bin/env python3
"""
convert_for_display.py

Converts images or videos to the specs required by the Meta Ray-Ban glasses display.
Output is written to ../knockout_content/ (relative to this script).

Usage:
    python convert_for_display.py <path> [<path> ...]

Output filenames:
    <stem>_display.png   (images)
    <stem>_display.mp4   (videos)

Image spec (sent over BTC, no WiFi needed):
    Any format → JPEG, fit within 400×400, aspect ratio preserved.
    Quality 85. Target file size: < 100 KB — flagged with ! if missed.

Video spec (fetched by glasses over WiFi):
    - H.264 Constrained Baseline profile, Level 3.0
    - Max 400 px per side, max 70,000 total pixels (≈ 265×265 square)
    - faststart moov atom (required for streaming)
    - Silent mono AAC audio track at 8 kbps / 22050 Hz (~5 KB per 5 s clip) — provides
      audio clock reference to encourage real-time playback on the glasses
    - No tmcd timecode track (causes playback issues)
    - yuv420p pixel format
    - Shorter clips (≤ 10 s) stream more smoothly than longer ones
"""

import json
import math
import subprocess
import sys
from pathlib import Path

# ---------- config ----------
IMAGE_MAX_SIDE   = 400
VIDEO_MAX_SIDE   = 200
VIDEO_MAX_PIXELS = 70_000
OUTPUT_DIR       = Path(__file__).resolve().parent.parent / "knockout_content"

IMAGE_EXTS = {".avif", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}
# ----------------------------


def output_stem(src: Path) -> str:
    """Returns the stem used for the output file, stripping an existing _display suffix."""
    stem = src.stem
    if stem.endswith("_display"):
        stem = stem[: -len("_display")]
    return stem


def convert_image(src: Path, dst: Path) -> None:
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow is required for image conversion: pip install pillow")

    img = Image.open(src).convert("RGB")
    orig_w, orig_h = img.size
    img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), Image.LANCZOS)
    img.save(dst, "JPEG", quality=85, optimize=True)
    size_kb = dst.stat().st_size / 1024
    flag = " !" if size_kb > 100 else ""
    print(f"  {orig_w}x{orig_h} -> {img.size[0]}x{img.size[1]}  {size_kb:.1f} KB{flag}  saved: {dst.name}")


def get_video_dims(src: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(src),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed:\n{result.stderr.strip()}")
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("ffprobe found no video stream")
    return int(streams[0]["width"]), int(streams[0]["height"])


def calc_target_dims(w: int, h: int) -> tuple[int, int]:
    """
    Scales (w, h) down to satisfy both the per-side and total-pixel constraints,
    rounding to even dimensions for H.264 compatibility.
    """
    scale = 1.0
    if max(w, h) > VIDEO_MAX_SIDE:
        scale = min(scale, VIDEO_MAX_SIDE / max(w, h))
    if w * h * scale * scale > VIDEO_MAX_PIXELS:
        scale = min(scale, math.sqrt(VIDEO_MAX_PIXELS / (w * h)))
    # Round down to nearest even (H.264 requires even dimensions)
    new_w = max(2, int(w * scale) // 2 * 2)
    new_h = max(2, int(h * scale) // 2 * 2)
    return new_w, new_h


def convert_video(src: Path, dst: Path) -> None:
    w, h = get_video_dims(src)
    tw, th = calc_target_dims(w, h)
    print(f"  source: {w}x{h} ({w*h:,} px)  ->  target: {tw}x{th} ({tw*th:,} px)")

    cmd = [
        "ffmpeg",
        "-i", str(src),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=22050",
        "-c:v", "libx264",
        "-level", "3.0",
        "-bf", "0",                  # no B-frames — simpler decode path for glasses HW decoder
        "-refs", "1",                # single reference frame — reduces decoder buffer demand
        "-b:v", "50k",               # target bitrate — sized for BT transport headroom
        "-maxrate", "75k",           # hard ceiling on instantaneous bitrate
        "-bufsize", "150k",          # 2× maxrate VBV buffer — smooths bitrate spikes across scene changes
        "-vf", f"scale={tw}:{th},setsar=1",
        "-r", "15",                  # 15 fps — doubles per-frame decode budget vs 30
        "-g", "105",                 # keyframe every 7 s at 15 fps — one initial IDR for most short clips
        "-c:a", "aac",               # silent mono audio track — provides clock reference for real-time playback
        "-b:a", "8k",                # 8 kbps — ~5 KB overhead for a 5 s clip
        "-ac", "1",                  # mono
        "-ar", "22050",              # low sample rate to minimise data
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",                 # stop encoding when the video (shortest input) ends
        "-map_chapters", "-1",       # strip chapter metadata
        "-movflags", "+faststart",   # moov atom at front (required for streaming)
        "-pix_fmt", "yuv420p",
        "-y",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ffmpeg stderr:\n{result.stderr[-2000:]}")
        raise RuntimeError("ffmpeg conversion failed")
    print(f"  saved: {dst.name}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for arg in sys.argv[1:]:
        src = Path(arg)
        if not src.is_absolute():
            src = Path.cwd() / src
        src = src.resolve()

        if not src.exists():
            print(f"[skip] not found: {arg}")
            continue

        ext = src.suffix.lower()
        stem = output_stem(src)

        print(f"\n[{'image' if ext in IMAGE_EXTS else 'video' if ext in VIDEO_EXTS else '?'}] {src.name}")

        try:
            if ext in IMAGE_EXTS:
                dst = OUTPUT_DIR / f"{stem}_display.jpg"
                convert_image(src, dst)

            elif ext in VIDEO_EXTS:
                dst = OUTPUT_DIR / f"{stem}_display.mp4"
                convert_video(src, dst)

            else:
                print(f"  skipped — unrecognised extension ({ext})")
                print(f"  supported: {sorted(IMAGE_EXTS | VIDEO_EXTS)}")

        except Exception as e:
            print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
