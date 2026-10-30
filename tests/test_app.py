import io
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "WORKDIR", tmp_path / "work")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def upload_jpeg():
    img = Image.new("RGB", (60, 40), (1, 2, 3))
    exif = Image.Exif()
    exif[271] = "TestCamera"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return (io.BytesIO(buf.getvalue()), "photo.jpg")


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_index_shows_form(client):
    body = client.get("/").get_data(as_text=True)
    assert "speedpicture" in body and 'name="files"' in body


def test_process_strips_and_offers_downloads(client):
    resp = client.post(
        "/process",
        data={"mode": "quality", "files": upload_jpeg()},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "メタ情報" in body and "ダウンロード" in body
    work = app_module.WORKDIR
    batch = next(p.name for p in work.iterdir() if p.is_dir())
    dl = client.get(f"/download/{batch}/photo.jpg")
    assert dl.status_code == 200
    with Image.open(io.BytesIO(dl.data)) as out:
        assert len(out.getexif()) == 0
    zipped = client.get(f"/download-all/{batch}")
    assert zipped.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zipped.data)) as zf:
        assert zf.namelist() == ["photo.jpg"]


def test_process_without_files_redirects(client):
    resp = client.post("/process", data={"mode": "quality"})
    assert resp.status_code in (302, 400)


def test_download_rejects_bad_batch(client):
    assert client.get("/download/nope/photo.jpg").status_code == 404
