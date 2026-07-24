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
