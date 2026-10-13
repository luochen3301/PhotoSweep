from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Detection:
    category: str
    confidence: int
    reasons: list[str]
    detector_version: str
    app_source: str | None = None


SCREENSHOT_NAME = re.compile(
    r"(screenshot|screen[\s_-]?shot|截屏|截图|长截图|scrollshot|screencapture)",
    re.IGNORECASE,
)
SCREENSHOT_PATH = re.compile(r"(^|[/\\])(screenshots?|截屏|截图)([/\\]|$)", re.IGNORECASE)
RECORDING_NAME = re.compile(
    r"(screen[\s_-]?record(?:ing)?|screenrecord|屏幕录制|屏幕录像|录屏)",
    re.IGNORECASE,
)
RECORDING_PATH = re.compile(
    r"(^|[/\\])(screen[\s_-]?recordings?|屏幕录制|录屏)([/\\]|$)", re.IGNORECASE
)
APP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "WeChat",
        re.compile(
            r"(^|[/\\])(micromsg|wechat|weixin|微信)([/\\]|$)|"
            r"(^|[/\\])(mmexport\d+|wx_camera[_\d-])",
            re.IGNORECASE,
        ),
    ),
    (
        "QQ",
        re.compile(
            r"(^|[/\\])(mobileqq|qq_images?|qqfile|腾讯qq|qq空间)([/\\]|$)|"
            r"(^|[/\\])(qq_pic_merged_|qq图片\d+|qqphoto[_\d])",
            re.IGNORECASE,
        ),
    ),
    (
        "Other",
        re.compile(
            r"(^|[/\\])(telegram|whatsapp(?: images)?|messenger|signal|line)([/\\]|$)|"
            r"(^|[/\\])IMG-\d{8}-WA\d+",
            re.IGNORECASE,
        ),
    ),
]

DETECTOR_VERSIONS = {
    "screenshot": "screenshot-v2",
    "screen_recording": "recording-v2",
    "app_image": "app-image-v2",
}


def screenshot_detection(
    *,
    filename: str,
    relative_path: str,
    extension: str,
    width: int | None,
    height: int | None,
    camera_make: str | None,
    camera_model: str | None,
) -> Detection | None:
    score = 0
    reasons: list[str] = []
    if SCREENSHOT_NAME.search(filename):
        score += 45
        reasons.append("文件名符合截图规则")
    if SCREENSHOT_PATH.search(relative_path):
        score += 35
        reasons.append("目录名称表明这是截图")
    if not camera_make and not camera_model:
        score += 15
        reasons.append("无相机 EXIF")
    else:
        score -= 40
        reasons.append("包含相机 EXIF，降低截图置信度")
    if width and height:
        short, long = sorted((width, height))
        ratio = long / max(short, 1)
        if 320 <= short <= 2160 and 1.25 <= ratio <= 4.5:
            score += 20
            reasons.append("分辨率符合手机屏幕比例")
        elif ratio > 4.5 and 320 <= short <= 2160:
            score += 25
            reasons.append("尺寸符合长截图特征")
    if extension.lower() in {".png", ".webp", ".heic", ".heif"}:
        score += 5
        reasons.append("格式符合常见截图格式")
    if score >= 60:
        return Detection("screenshot", min(score, 100), reasons, DETECTOR_VERSIONS["screenshot"])
    return None


def screen_recording_detection(
    *,
    filename: str,
    relative_path: str,
    width: int | None,
    height: int | None,
    fps: float | None,
    codec: str | None,
    camera_make: str | None = None,
    camera_model: str | None = None,
) -> Detection | None:
    score = 0
    reasons: list[str] = []
    if RECORDING_NAME.search(filename):
        score += 60
        reasons.append("文件名符合屏幕录制规则")
    if RECORDING_PATH.search(relative_path):
        score += 45
        reasons.append("目录名称表明这是屏幕录制")
    if width and height:
        short, long = sorted((width, height))
        ratio = long / max(short, 1)
        if 320 <= short <= 2160 and 1.25 <= ratio <= 3.5:
            score += 15
            reasons.append("分辨率符合手机屏幕比例")
    if fps and 20 <= fps <= 120:
        score += 10
        reasons.append("帧率符合常见屏幕录制")
    if codec and codec.lower() in {"h264", "hevc", "av1", "vp9"}:
        score += 5
        reasons.append("视频编码符合常见手机录屏")
    if camera_make or camera_model:
        score -= 40
        reasons.append("包含相机信息，降低录屏置信度")
    if score >= 60:
        return Detection(
            "screen_recording", min(score, 100), reasons, DETECTOR_VERSIONS["screen_recording"]
        )
    return None


def app_image_detection(filename: str, relative_path: str) -> Detection | None:
    haystack = f"{relative_path}/{filename}"
    for source, pattern in APP_PATTERNS:
        if pattern.search(haystack):
            label = "微信" if source == "WeChat" else source
            return Detection(
                "app_image",
                92 if source in {"WeChat", "QQ"} else 82,
                [f"文件名或目录符合 {label} 保存规则"],
                DETECTOR_VERSIONS["app_image"],
                source,
            )
    return None


def detect_media(
    *,
    media_type: str,
    filename: str,
    relative_path: str,
    extension: str,
    width: int | None,
    height: int | None,
    camera_make: str | None = None,
    camera_model: str | None = None,
    fps: float | None = None,
    codec: str | None = None,
) -> list[Detection]:
    detections: list[Detection] = []
    if media_type == "photo":
        screenshot = screenshot_detection(
            filename=filename,
            relative_path=relative_path,
            extension=extension,
            width=width,
            height=height,
            camera_make=camera_make,
            camera_model=camera_model,
        )
        if screenshot:
            detections.append(screenshot)
        app_image = app_image_detection(filename, relative_path)
        if app_image:
            detections.append(app_image)
    elif media_type == "video":
        recording = screen_recording_detection(
            filename=filename,
            relative_path=relative_path,
            width=width,
            height=height,
            fps=fps,
            codec=codec,
            camera_make=camera_make,
            camera_model=camera_model,
        )
        if recording:
            detections.append(recording)
    return detections
