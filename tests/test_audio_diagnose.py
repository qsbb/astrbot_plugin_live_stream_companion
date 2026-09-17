# -*- coding: utf-8 -*-
"""语音诊断（拓展页「生成试听」）纯函数测试。"""

import base64
import pathlib
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

try:  # AstrBot 运行环境（容器内 unittest / pytest）
    from data.plugins.astrbot_plugin_live_stream_companion.audio_diagnose import (
        build_diagnose_checks,
        empty_audio_info,
        inline_audio_data_url,
        passed_check_count,
        probe_audio_file,
    )
except ModuleNotFoundError:  # 本地直接跑单测：该模块不依赖 AstrBot
    from audio_diagnose import (  # type: ignore[no-redef]
        build_diagnose_checks,
        empty_audio_info,
        inline_audio_data_url,
        passed_check_count,
        probe_audio_file,
    )


def _write_wav(path: Path, *, seconds: float = 0.5, rate: int = 16000, channels: int = 1) -> Path:
    frames = int(rate * seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        payload = b"".join(struct.pack("<h", 0) for _ in range(frames * channels))
        handle.writeframes(payload)
    return path


class ProbeAudioFileTests(unittest.TestCase):
    def test_reads_wav_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_wav(Path(tmp) / "diag.wav", seconds=0.75, rate=24000, channels=1)
            info = probe_audio_file(path)
        self.assertEqual(info["format"], "WAV")
        self.assertEqual(info["mime"], "audio/wav")
        self.assertEqual(info["name"], "diag.wav")
        self.assertEqual(info["sample_rate"], 24000)
        self.assertEqual(info["channels"], 1)
        self.assertTrue(info["duration_known"])
        self.assertAlmostEqual(info["duration_seconds"], 0.75, places=3)
        self.assertGreater(info["size_bytes"], 0)
        self.assertEqual(info["error"], "")

    def test_missing_file_reports_error(self):
        info = probe_audio_file("/tmp/definitely-not-here-diag.wav")
        self.assertFalse(info["duration_known"])
        self.assertTrue(info["error"])

    def test_non_wav_keeps_metadata_without_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diag.mp3"
            path.write_bytes(b"not-a-real-mp3")
            info = probe_audio_file(path)
        self.assertEqual(info["format"], "MP3")
        self.assertEqual(info["mime"], "audio/mpeg")
        self.assertFalse(info["duration_known"])
        self.assertEqual(info["duration_seconds"], 0.0)
        self.assertGreater(info["size_bytes"], 0)

    def test_empty_audio_info_shape(self):
        info = empty_audio_info()
        self.assertEqual(info["path"], "")
        self.assertFalse(info["duration_known"])
        self.assertEqual(info["size_bytes"], 0)


class InlineAudioTests(unittest.TestCase):
    def test_inlines_small_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_wav(Path(tmp) / "small.wav", seconds=0.1)
            data_url = inline_audio_data_url(path)
            raw = path.read_bytes()
        self.assertTrue(data_url.startswith("data:audio/wav;base64,"))
        encoded = data_url.split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), raw)

    def test_skips_audio_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_wav(Path(tmp) / "big.wav", seconds=0.5)
            self.assertEqual(inline_audio_data_url(path, max_bytes=16), "")

    def test_missing_path_returns_empty(self):
        self.assertEqual(inline_audio_data_url(""), "")
        self.assertEqual(inline_audio_data_url("/tmp/not-here-diag.wav"), "")


class DiagnoseChecksTests(unittest.TestCase):
    def test_all_green_when_external_service_works(self):
        checks = build_diagnose_checks(
            provider_available=True,
            external_configured=True,
            external_resolved=True,
            external_target="demo_plugin / demo_tool / synthesize_speech",
            audio_ok=True,
            duration_known=True,
        )
        self.assertEqual(passed_check_count(checks), len(checks))
        labels = [item["label"] for item in checks]
        self.assertIn("外部注册服务命中", labels)
        self.assertNotIn("回退策略生效", labels)

    def test_reports_reasons_when_external_service_missing(self):
        checks = build_diagnose_checks(
            provider_available=True,
            external_configured=True,
            external_resolved=False,
            external_error="未找到已配置的外部 TTS 服务",
            audio_ok=False,
            duration_known=False,
        )
        by_label = {item["label"]: item for item in checks}
        self.assertFalse(by_label["外部注册服务命中"]["ok"])
        self.assertEqual(by_label["外部注册服务命中"]["note"], "未找到已配置的外部 TTS 服务")
        self.assertFalse(by_label["返回音频路径"]["ok"])
        self.assertFalse(by_label["时长可解析（嘴型 / 字幕可用）"]["ok"])
        self.assertTrue(by_label["会话 TTS Provider 可用"]["ok"])
        self.assertEqual(passed_check_count(checks), 1)

    def test_marks_fallback_as_passed(self):
        checks = build_diagnose_checks(
            provider_available=True,
            external_configured=True,
            external_resolved=True,
            fallback_used=True,
            audio_ok=True,
            duration_known=True,
        )
        by_label = {item["label"]: item for item in checks}
        self.assertTrue(by_label["回退策略生效"]["ok"])

    def test_provider_missing_is_reported(self):
        checks = build_diagnose_checks(provider_available=False, audio_ok=False, duration_known=False)
        by_label = {item["label"]: item for item in checks}
        self.assertFalse(by_label["会话 TTS Provider 可用"]["ok"])
        self.assertIn("TTS Provider", by_label["会话 TTS Provider 可用"]["note"])


# ---- 拓展页接口接线（需要 AstrBot 运行环境，本地跳过） ----
try:  # pragma: no cover - 仅在容器内可用
    from data.plugins.astrbot_plugin_live_stream_companion.page_api import (
        LiveStreamCompanionPageApi,
    )
except ModuleNotFoundError:  # 本地没有 AstrBot，相关用例自动跳过
    LiveStreamCompanionPageApi = None  # type: ignore[assignment]


class _StubContext:
    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, methods, desc))


class _StubPlugin:
    def __init__(self):
        self.context = _StubContext()
        self.context.routes = []
        self.config = {}


@unittest.skipUnless(LiveStreamCompanionPageApi is not None, "需要 AstrBot 运行环境")
class DiagnoseRouteTests(unittest.TestCase):
    def test_tts_diagnose_route_is_registered(self):
        plugin = _StubPlugin()
        LiveStreamCompanionPageApi(plugin).register_routes()
        routes = {path: methods for path, methods, _desc in plugin.context.routes}
        self.assertIn(
            "/astrbot_plugin_live_stream_companion/page/tts/diagnose",
            routes,
        )
        self.assertEqual(routes["/astrbot_plugin_live_stream_companion/page/tts/diagnose"], ["POST"])


if __name__ == "__main__":
    unittest.main()
