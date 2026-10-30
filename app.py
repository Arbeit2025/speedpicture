"""speedpicture: strip metadata + shrink photos/videos, then upload
the cleaned files to Google Photos yourself — smaller files upload faster.

Processing happens on this server; batches are kept in a temp workdir
and pruned after one hour. For sensitive photos, run it locally
(`python app.py`) so files never leave your machine.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from flask import Flask, abort, redirect, render_template_string, request, send_file

from processor import MODES, process_bytes

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300 MB per request

WORKDIR = Path(os.environ.get("SPEEDPICTURE_WORKDIR", Path(__file__).parent / "work"))
BATCH_TTL_SECONDS = 3600

CSS = """
body{font-family:system-ui,sans-serif;margin:2em auto;max-width:44em;padding:0 1em;line-height:1.6}
h1{font-size:1.5em} table{border-collapse:collapse;width:100%}
td,th{border:1px solid #ccc;padding:.35em .5em;font-size:.92em}
.err{color:#b00020} .ok{color:#137333} .muted{color:#666;font-size:.9em}
button,input[type=submit]{font-size:1em;padding:.5em 1.2em}
"""

INDEX = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>speedpicture</title><style>{{css}}</style></head><body>
<h1>📷 speedpicture</h1>
<p>写真・動画の<strong>メタ情報（位置情報・撮影日時・機器情報など）を全部消して</strong>、
ついでに小さくして、Googleフォトへのアップロードを速くするための準備ツールです。</p>
<form method="post" action="/process" enctype="multipart/form-data">
<p><input type="file" name="files" multiple required
 accept=".jpg,.jpeg,.png,.webp,.mp4,.mov,.m4v,.webm,.mkv"></p>
<p>モード:
<label><input type="radio" name="mode" value="quality" checked>
画質そのまま（メタ情報だけ削除）</label>
<label><input type="radio" name="mode" value="fast">
高速（長辺2048px・JPEG画質80まで圧縮）</label></p>
<p><input type="submit" value="メタ情報を消して変換"></p>
</form>
<p class="muted">変換後のファイルをダウンロードして、Googleフォトのアプリ／Webから
アップロードしてください。ファイルが小さい分、転送が速くなります。
処理したファイルはサーバーに1時間だけ置かれ、自動で消えます。
位置情報を含む写真を公開サーバーで処理したくない場合は、このツールを
自分のPCで動かしてください（README参照）。</p>
</body></html>"""

RESULT = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>変換結果 - speedpicture</title><style>{{css}}</style></head><body>
<h1>変換結果</h1>
<table><tr><th>ファイル</th><th>結果</th><th>サイズ</th><th></th></tr>
{% for r in results %}
<tr><td>{{r.name}}</td>
{% if r.ok %}<td class="ok">{{r.detail}}</td>
<td>{{r.size_before}} → {{r.size_after}} bytes（{{"%.1f"|format(r.reduction_pct)}}%減）</td>
<td><a href="/download/{{batch}}/{{r.name}}">ダウンロード</a></td>
{% else %}<td class="err" colspan="2">失敗: {{r.error}}</td><td></td>{% endif %}
</tr>{% endfor %}
</table>
<p>合計: {{total_before}} → {{total_after}} bytes</p>
{% if ok_count %}<p><a href="/download-all/{{batch}}">✅ 全部まとめてZIPでダウンロード</a>
→ Googleフォトへアップロードしてください</p>{% endif %}
<p><a href="/">← もどる</a></p>
</body></html>"""


def prune_old_batches() -> None:
    if not WORKDIR.exists():
        return
    cutoff = time.time() - BATCH_TTL_SECONDS
    for child in WORKDIR.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def safe_name(raw: str, index: int) -> str:
    name = Path(raw or "").name
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._")
    return name or f"file_{index}"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    prune_old_batches()
    return render_template_string(INDEX, css=CSS)


@app.post("/process")
def process():
    prune_old_batches()
    mode = request.form.get("mode", "quality")
    if mode not in MODES:
        abort(400)
    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    if not uploads:
        return redirect("/")
    batch = uuid.uuid4().hex[:12]
    batch_dir = WORKDIR / batch
    batch_dir.mkdir(parents=True, exist_ok=True)
    results = []
    used: set[str] = set()
    for i, upload in enumerate(uploads):
        name = safe_name(upload.filename, i)
        if name in used:
            stem, dot, ext = name.rpartition(".")
            name = f"{stem}_2.{ext}" if dot else f"{name}_2"
            counter = 3
            while name in used:
                name = f"{stem}_{counter}.{ext}" if dot else f"{name}_{counter}"
                counter += 1
        used.add(name)
        result, _ = process_bytes(name, upload.read(), mode, batch_dir)
        results.append(result)
    ok_results = [r for r in results if r.ok]
    return render_template_string(
        RESULT,
        css=CSS,
        batch=batch,
        results=results,
        ok_count=len(ok_results),
        total_before=sum(r.size_before for r in ok_results),
        total_after=sum(r.size_after for r in ok_results),
    )


def batch_file(batch: str, name: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", batch or ""):
        abort(404)
    path = WORKDIR / batch / Path(name).name
    if not path.is_file():
        abort(404)
    return path


@app.get("/download/<batch>/<name>")
def download(batch: str, name: str):
    return send_file(batch_file(batch, name), as_attachment=True)


@app.get("/download-all/<batch>")
def download_all(batch: str):
    if not re.fullmatch(r"[0-9a-f]{12}", batch or ""):
        abort(404)
    batch_dir = WORKDIR / batch
    files = sorted(p for p in batch_dir.iterdir() if p.is_file()) if batch_dir.is_dir() else []
    if not files:
        abort(404)
    zip_path = batch_dir / "speedpicture.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if f != zip_path:
                zf.write(f, f.name)
    return send_file(zip_path, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
