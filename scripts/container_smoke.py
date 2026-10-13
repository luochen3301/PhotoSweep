"""Real NAS-like read-only mount smoke test. Use only with disposable CI volumes."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def seed() -> None:
    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener()
    root = Path("/fixtures")
    root.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1080, 1920), "#276c76")
    image.save(root / "Screenshot_1.png")
    image.save(root / "Screenshot_2.png")
    Image.new("RGB", (64, 96), "#124563").save(root / "phone.heic", format="HEIF")
    image.resize((40, 40)).save(root / "animation.gif")
    exif = Image.Exif()
    exif[271], exif[272], exif[274] = "TestCamera", "Fixture", 6
    Image.new("RGB", (100, 200), "#541233").save(root / "camera.jpg", exif=exif)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x640:r=30",
            "-t",
            "0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(root / "ScreenRecording.mp4"),
        ],
        check=True,
        timeout=45,
    )
    for path in root.iterdir():
        path.chmod(0o644)
    root.chmod(0o755)


def verify(persisted: bool) -> None:
    cookies = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))

    def request(path, body=None, method=None, expected=200):
        headers = {"Content-Type": "application/json"}
        csrf = next((cookie.value for cookie in cookies if cookie.name == "photosweep_csrf"), None)
        if csrf:
            headers["X-CSRF-Token"] = csrf
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            "http://127.0.0.1:8080/api/v1" + path, data=data, headers=headers, method=method
        )
        try:
            response = client.open(req, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            assert response.status == expected, (path, response.status, content)
            if response.headers.get_content_type() == "image/webp":
                assert content[:4] == b"RIFF" and content[8:12] == b"WEBP"
                return content
            return json.loads(content)

    credentials = {"username": "smoke", "password": "disposable-ci-password"}
    configured = request("/setup/status")["data"]["configured"]
    if persisted:
        assert configured, "Administrator was lost across restart"
    request(
        "/auth/login" if configured else "/setup/admin",
        credentials,
        expected=200 if configured else 201,
    )
    if persisted:
        assert request("/dashboard/summary")["data"]["photos"]["count"] == 5
        assert len(request("/libraries")["data"]) == 1
        print("Restart persistence verified")
        return
    library = request(
        "/libraries", {"name": "Read-only fixture", "root_path": "/media/photos"}, expected=201
    )["data"]
    assert library["writable"] is False

    def wait_job(job_id):
        for _ in range(300):
            job = request(f"/jobs/{job_id}")["data"]
            if job["status"] in {"completed", "failed", "interrupted", "cancelled"}:
                assert job["status"] == "completed", job
                assert job["result"]["errors"] == 0, job
                return job
            time.sleep(0.2)
        raise AssertionError("Job timed out")

    scan = request(f"/libraries/{library['id']}/scans", {"mode": "full"}, expected=202)["data"]
    wait_job(scan["id"])
    media = request("/media")["data"]
    assert len(media) == 6, media
    for item in media:
        assert not item["metadata_error"], item
        request(f"/media/{item['id']}/thumbnail?size=256")
    camera = next(item for item in media if item["filename"] == "camera.jpg")
    assert camera["camera_make"] == "TestCamera" and camera["width"] == 200
    summary = request("/dashboard/summary")["data"]
    assert summary["photos"]["count"] == 5 and summary["videos"]["count"] == 1
    assert summary["screen_recording"]["count"] == 1
    request("/trash/actions/move", {"media_ids": [media[0]["id"]]}, expected=409)
    run = request(
        "/duplicate-runs", {"library_ids": [library["id"]], "media_type": "both"}, expected=202
    )["data"]
    wait_job(run["job"]["id"])
    groups = request(f"/duplicate-groups?run_id={run['run_id']}")["data"]
    assert len(groups) == 1 and groups[0]["copy_count"] == 2, groups
    assert len(list(Path("/media/photos").iterdir())) == 6
    print(
        "Real HEIF/GIF/EXIF/video scan, WebP thumbnails, duplicate hashes "
        "and read-only guard verified"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--persisted", action="store_true")
    args = parser.parse_args()
    seed() if args.seed else verify(args.persisted)
