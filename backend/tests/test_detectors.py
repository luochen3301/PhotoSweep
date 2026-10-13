from app.detectors.rules import detect_media


def test_screenshot_combines_multiple_signals() -> None:
    detections = detect_media(
        media_type="photo",
        filename="Screenshot_20260724.png",
        relative_path="Mobile/Screenshots/Screenshot_20260724.png",
        extension=".png",
        width=1170,
        height=2532,
    )
    screenshot = next(item for item in detections if item.category == "screenshot")
    assert screenshot.confidence == 100
    assert "文件名符合截图规则" in screenshot.reasons
    assert "无相机 EXIF" in screenshot.reasons


def test_camera_photo_is_not_screenshot() -> None:
    detections = detect_media(
        media_type="photo",
        filename="DSC_0123.jpg",
        relative_path="2026/Holiday/DSC_0123.jpg",
        extension=".jpg",
        width=4000,
        height=3000,
        camera_make="SONY",
        camera_model="ILCE-7M4",
    )
    assert all(item.category != "screenshot" for item in detections)


def test_wechat_and_qq_patterns_are_review_only_classifications() -> None:
    wechat = detect_media(
        media_type="photo",
        filename="mmexport1753332200000.jpg",
        relative_path="Pictures/WeiXin/mmexport1753332200000.jpg",
        extension=".jpg",
        width=1080,
        height=1440,
    )
    app = next(item for item in wechat if item.category == "app_image")
    assert app.app_source == "WeChat"
    assert app.confidence >= 90


def test_app_patterns_do_not_match_arbitrary_words():
    for path in ["Holiday/Signal mountain.png", "wechat tips.png", "baseline/photo.png"]:
        detections = detect_media(
            media_type="photo",
            filename=path.split("/")[-1],
            relative_path=path,
            extension=".png",
            width=1000,
            height=1000,
        )
        assert all(item.category != "app_image" for item in detections)


def test_qq_and_other_tags_are_distinct():
    for path, expected in [
        ("QQ图片202610130000.jpg", "QQ"),
        ("WhatsApp Images/IMG-20261013-WA0001.jpg", "Other"),
        ("Telegram/holiday.jpg", "Other"),
    ]:
        detections = detect_media(
            media_type="photo",
            filename=path.split("/")[-1],
            relative_path=path,
            extension=".jpg",
            width=1000,
            height=1000,
        )
        assert (
            next(item for item in detections if item.category == "app_image").app_source == expected
        )


def test_screen_recording_has_evidence_and_camera_negative_signal():
    fields = dict(
        media_type="video",
        filename="ScreenRecording.mp4",
        relative_path="video.mp4",
        extension=".mp4",
        width=1080,
        height=1920,
        fps=30,
        codec="h264",
    )
    detected = detect_media(**fields)
    assert detected[0].category == "screen_recording" and detected[0].confidence == 90
    assert len(detected[0].reasons) == 4
    assert not detect_media(**fields, camera_make="Apple", camera_model="iPhone")
