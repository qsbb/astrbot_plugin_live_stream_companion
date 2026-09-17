# -*- coding: utf-8 -*-
"""拓展页「语音诊断 / 生成试听」用的纯工具函数（不依赖 AstrBot，方便单测）。

只做三件事：

* 读取音频文件的基础参数（格式 / 采样率 / 声道 / 时长 / 体积），用来判断这条
  TTS 结果能不能配合嘴型与打字机字幕；
* 把一段诊断结果整理成「检查清单」（哪些环节通过、哪些需要留意）；
* 把音频内联成 data URL，方便拓展页直接试听（超过体积上限就只给参数不给音频）。
"""
from __future__ import annotations

import base64
import os
import wave
from pathlib import Path
from typing import Any

# 超过这个体积就不再内联给拓展页播放，避免把页面响应撑大
MAX_INLINE_AUDIO_BYTES = 4 * 1024 * 1024

AUDIO_MIME_TYPES = {
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def audio_mime_type(path: Any) -> str:
    """按扩展名推断 MIME，未知类型回退成二进制流。"""
    suffix = Path(str(path or "")).suffix.lower()
    return AUDIO_MIME_TYPES.get(suffix, "application/octet-stream")


def empty_audio_info(path: Any = "") -> dict[str, Any]:
    return {
        "path": str(path or ""),
        "name": "",
        "format": "",
        "mime": "application/octet-stream",
        "sample_rate": 0,
        "channels": 0,
        "duration_seconds": 0.0,
        "size_bytes": 0,
        "duration_known": False,
        "error": "",
    }


def probe_audio_file(path: Any) -> dict[str, Any]:
    """读取音频参数；读不到时长时 ``duration_known`` 为 ``False``。"""
    resolved = str(path or "").strip()
    info = empty_audio_info(resolved)
    if not resolved:
        info["error"] = "音频路径为空"
        return info
    file_path = Path(resolved)
    info["name"] = file_path.name
    info["format"] = file_path.suffix.lstrip(".").upper()
    info["mime"] = audio_mime_type(resolved)
    try:
        info["size_bytes"] = int(os.path.getsize(file_path))
    except OSError as exc:
        info["error"] = f"读取音频文件失败：{exc}"
        return info
    if file_path.suffix.lower() not in {".wav", ".wave"}:
        # 其他容器格式只给参数，时长交给播放端
        return info
    try:
        with wave.open(str(file_path), "rb") as wav:
            channels = max(1, int(wav.getnchannels()))
            rate = max(1, int(wav.getframerate()))
            frames = max(0, int(wav.getnframes()))
    except Exception as exc:  # 非 PCM / 头损坏的 wav 读不出时长
        info["error"] = f"读取 WAV 参数失败：{exc}"
        return info
    info["channels"] = channels
    info["sample_rate"] = rate
    if frames and rate:
        info["duration_seconds"] = round(frames / rate, 3)
        info["duration_known"] = True
    return info


def inline_audio_data_url(path: Any, *, max_bytes: int = MAX_INLINE_AUDIO_BYTES) -> str:
    """把音频读成 data URL；超限或读取失败时返回空串。"""
    resolved = str(path or "").strip()
    if not resolved:
        return ""
    try:
        size = int(os.path.getsize(resolved))
        if size <= 0 or size > max(1, int(max_bytes)):
            return ""
        with open(resolved, "rb") as handle:
            payload = handle.read()
    except OSError:
        return ""
    if not payload:
        return ""
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{audio_mime_type(resolved)};base64,{encoded}"


def build_diagnose_checks(
    *,
    provider_available: bool,
    external_configured: bool = False,
    external_resolved: bool = False,
    external_error: str = "",
    external_target: str = "",
    fallback_used: bool = False,
    audio_ok: bool = False,
    duration_known: bool = False,
) -> list[dict[str, Any]]:
    """组装诊断检查清单，供拓展页逐项显示。"""
    checks: list[dict[str, Any]] = [
        {
            "label": "会话 TTS Provider 可用",
            "ok": bool(provider_available),
            "note": "" if provider_available else "当前会话没有可用的 TTS Provider",
        }
    ]
    if external_configured:
        if external_resolved:
            resolved_note = f"已解析到：{external_target}" if external_target else ""
            checks.append({"label": "外部注册服务命中", "ok": True, "note": resolved_note})
        else:
            checks.append(
                {
                    "label": "外部注册服务命中",
                    "ok": False,
                    "note": external_error or "没有命中配置的外部服务",
                }
            )
    if fallback_used:
        checks.append(
            {
                "label": "回退策略生效",
                "ok": True,
                "note": "外部服务没产出音频，已改用会话 TTS Provider",
            }
        )
    checks.append(
        {
            "label": "返回音频路径",
            "ok": bool(audio_ok),
            "note": "" if audio_ok else "合成结束但没有拿到音频文件",
        }
    )
    checks.append(
        {
            "label": "时长可解析（嘴型 / 字幕可用）",
            "ok": bool(duration_known),
            "note": "" if duration_known else "只拿到文件路径，读不到时长，嘴型联动会跳过这条音频",
        }
    )
    return checks


def passed_check_count(checks: list[dict[str, Any]]) -> int:
    return sum(1 for item in checks if item.get("ok"))
