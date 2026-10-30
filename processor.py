"""Core processing for speedpicture.

Two jobs, one pass over each file:

1. Strip metadata completely (EXIF / GPS / text chunks in images,
   container metadata and chapters in videos).
2. Optionally shrink images ("fast" mode) so the cleaned files are
   quicker to upload to Google Photos afterwards.

No file is ever passed through unchanged on failure: if a file cannot
be cleaned it is reported as an error, never silently copied.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

MODES = ("quality", "fast")
FAST_MAX_EDGE = 2048
FAST_QUALITY = 80
QUALITY_QUALITY = 95


@dataclass
class ProcessResult:
    name: str
    ok: bool
    size_before: int = 0
    size_after: int = 0
    detail: str = ""
    metadata_found: int = 0
    error: str = ""

    @property
    def reduction_pct(self) -> float:
        if not self.ok or self.size_before <= 0:
            return 0.0
        return (1 - self.size_after / self.size_before) * 100


def count_image_metadata(img: Image.Image) -> int:
    """Rough count of metadata items present before stripping."""
    count = 0
    try:
        count += len(img.getexif())
    except Exception:
        pass
    if img.format == "PNG" and getattr(img, "text", None):
        count += len(img.text)
    for key in ("icc_profile", "exif", "xmp", "comment"):
        if img.info.get(key):
            count += 1
    return count


def _clean_pixels(img: Image.Image) -> Image.Image:
    """Rebuild an image from raw pixels so nothing metadata-like survives."""
    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    if img.mode not in ("RGB", "RGBA", "L", "LA", "CMYK"):
        img = img.convert("RGB")
    clean = Image.frombytes(img.mode, img.size, img.tobytes())
    return clean


def strip_image(data: bytes, mode: str = "quality") -> tuple[bytes, dict]:
    """Return (cleaned_bytes, info). Raises ValueError on bad input."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    with Image.open(io.BytesIO(data)) as src:
        fmt = (src.format or "").upper()
        if fmt not in ("JPEG", "PNG", "WEBP"):
            raise ValueError(f"unsupported image format: {fmt or 'unknown'}")
        found = count_image_metadata(src)
        # Apply EXIF orientation to the pixels *before* dropping the EXIF,
        # otherwise portrait photos come back rotated.
        transposed = ImageOps.exif_transpose(src)
        width_before, height_before = transposed.size
        img = _clean_pixels(transposed)
        if mode == "fast":
            img.thumbnail((FAST_MAX_EDGE, FAST_MAX_EDGE), Image.LANCZOS)
        out = io.BytesIO()
        if fmt == "JPEG":
            if img.mode in ("RGBA", "LA"):
                img = img.convert("RGB")
            quality = FAST_QUALITY if mode == "fast" else QUALITY_QUALITY
            img.save(out, "JPEG", quality=quality, optimize=True)
        elif fmt == "PNG":
            img.save(out, "PNG", optimize=True)
        else:  # WEBP
            quality = FAST_QUALITY if mode == "fast" else 95
            img.save(out, "WEBP", quality=quality)
    return out.getvalue(), {
        "format": fmt,
        "metadata_found": found,
        "width_before": width_before,
        "height_before": height_before,
        "width_after": img.size[0],
        "height_after": img.size[1],
    }


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def read_video_metadata(path: Path) -> dict:
    """Container/format tags of a video, via ffprobe ({} if unavailable)."""
    if shutil.which("ffprobe") is None:
        return {}
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format_tags",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return {}
    try:
        tags = json.loads(proc.stdout).get("format", {}).get("tags", {})
    except (ValueError, AttributeError):
        return {}
    return {str(k): str(v) for k, v in tags.items()}


def strip_video(src: Path, dst: Path) -> dict:
    """Strip container metadata/chapters with a stream copy (no re-encode).

    Raises RuntimeError if ffmpeg is missing or fails.
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg が見つからないため動画を処理できません")
    before = read_video_metadata(src)
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-map", "0", "-map_metadata", "-1", "-map_chapters", "-1",
            "-c", "copy", str(dst),
        ],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        message = (proc.stderr or "ffmpeg failed").strip().splitlines()
        raise RuntimeError(message[-1] if message else "ffmpeg failed")
    return {"format": src.suffix.lstrip(".").upper(), "metadata_found": len(before)}


def process_bytes(name: str, data: bytes, mode: str, workdir: Path) -> tuple[ProcessResult, Path | None]:
    """Clean one uploaded file. Returns (result, output_path_or_None)."""
    safe = Path(name).name or "file"
    ext = Path(safe).suffix.lower()
    result = ProcessResult(name=safe, ok=False, size_before=len(data))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        if ext in IMAGE_EXTS:
            cleaned, info = strip_image(data, mode)
            out_path = workdir / safe
            out_path.write_bytes(cleaned)
            result.ok = True
            result.size_after = len(cleaned)
            result.metadata_found = info["metadata_found"]
            downsized = (info["width_after"], info["height_after"]) != (
                info["width_before"], info["height_before"])
            result.detail = (
                f"{info['format']} / メタ情報 {info['metadata_found']}件を削除"
                + (f" / {info['width_before']}x{info['height_before']} → "
                   f"{info['width_after']}x{info['height_after']} に縮小" if downsized else "")
            )
            return result, out_path
        if ext in VIDEO_EXTS:
            if mode == "fast":
                result.detail = "動画は高速モードでも再圧縮しません（メタ情報の削除のみ）"
            src_path = workdir / f"src_{safe}"
            out_path = workdir / safe
            src_path.write_bytes(data)
            try:
                info = strip_video(src_path, out_path)
            finally:
                src_path.unlink(missing_ok=True)
            result.ok = True
            result.size_after = out_path.stat().st_size
            result.metadata_found = info["metadata_found"]
            result.detail = (
                f"{info['format']} / メタ情報 {info['metadata_found']}件を削除（再圧縮なし） "
                + result.detail
            ).strip()
            return result, out_path
        result.error = f"未対応の形式です: {ext or '(拡張子なし)'}"
        return result, None
    except Exception as exc:  # report, never pass the original through
        result.error = str(exc)
        return result, None
