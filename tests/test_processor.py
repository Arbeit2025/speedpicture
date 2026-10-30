import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import processor


def jpeg_with_exif(size=(120, 80), orientation=None):
    img = Image.new("RGB", size, (200, 60, 30))
    exif = Image.Exif()
    exif[271] = "TestCamera"      # Make
    exif[305] = "TestSoftware"    # Software
    if orientation is not None:
        exif[274] = orientation   # Orientation
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_strip_jpeg_removes_exif_and_keeps_size():
    data = jpeg_with_exif()
    cleaned, info = processor.strip_image(data, "quality")
    assert info["metadata_found"] >= 2
    with Image.open(io.BytesIO(cleaned)) as out:
        assert len(out.getexif()) == 0
        assert out.size == (120, 80)


def test_orientation_is_applied_before_stripping():
    # Orientation 6 means "rotate 90 CW": stored 120x80 displays as 80x120.
    data = jpeg_with_exif(size=(120, 80), orientation=6)
    cleaned, info = processor.strip_image(data, "quality")
    assert (info["width_after"], info["height_after"]) == (80, 120)
    with Image.open(io.BytesIO(cleaned)) as out:
        assert len(out.getexif()) == 0


def test_fast_mode_resizes_and_shrinks():
    # Noise compresses badly at q95 and much better at q80 + smaller size.
    import random
    random.seed(42)
    img = Image.new("RGB", (800, 600))
    img.putdata([(random.randrange(256), random.randrange(256), random.randrange(256))
                 for _ in range(800 * 600)])
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    data = buf.getvalue()
    quality, _ = processor.strip_image(data, "quality")
    fast, info = processor.strip_image(data, "fast")
    assert max(info["width_after"], info["height_after"]) <= 2048
    small, info2 = processor.strip_image(data, "fast")
    assert info2["width_after"] == 800  # already under the cap: untouched dims
    assert len(fast) < len(quality)


def test_fast_mode_downsizes_large_image():
    data = jpeg_with_exif(size=(3000, 2000))
    _, info = processor.strip_image(data, "fast")
    assert (info["width_after"], info["height_after"]) == (2048, 1365)


def test_png_text_chunk_is_removed():
    img = Image.new("RGB", (40, 40), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=_text_info())
    cleaned, info = processor.strip_image(buf.getvalue(), "quality")
    assert info["metadata_found"] >= 1
    with Image.open(io.BytesIO(cleaned)) as out:
        assert not getattr(out, "text", {})


def _text_info():
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text("Comment", "secret location note")
    return info


def test_unsupported_image_format_raises():
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, "GIF")
    with pytest.raises(ValueError):
        processor.strip_image(gif.getvalue(), "quality")


def test_bad_mode_raises():
    with pytest.raises(ValueError):
        processor.strip_image(jpeg_with_exif(), "turbo")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_strip_video_removes_metadata(tmp_path):
    src = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
         "-metadata", "title=secret-title", "-metadata", "location=somewhere",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    assert processor.read_video_metadata(src).get("title") == "secret-title"
    dst = tmp_path / "out.mp4"
    info = processor.strip_video(src, dst)
    assert info["metadata_found"] >= 2
    after = processor.read_video_metadata(dst)
    assert "title" not in after and "location" not in after
    assert dst.stat().st_size > 0


def test_process_bytes_reports_unsupported(tmp_path):
    result, out = processor.process_bytes("memo.txt", b"hello", "quality", tmp_path)
    assert not result.ok and out is None and "未対応" in result.error


def test_process_bytes_never_passes_failed_file_through(tmp_path):
    result, out = processor.process_bytes("broken.jpg", b"not a jpeg", "quality", tmp_path)
    assert not result.ok and out is None
