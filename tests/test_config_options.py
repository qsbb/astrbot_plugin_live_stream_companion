# -*- coding: utf-8 -*-
"""拓展页下拉选项（外部 TTS / 远程 VTS）纯函数回归测试。"""

import unittest
from types import SimpleNamespace

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

try:  # AstrBot 运行环境（容器内 pytest / unittest）
    from data.plugins.astrbot_plugin_live_stream_companion.config_options import (
        build_external_tts_options,
        build_probe_targets,
        candidate_label,
        describe_external_tts_tool,
        derive_subnet_seeds,
        normalize_host_entries,
        plugin_display_name,
        plugin_identifiers,
        private_ipv4,
        subnet_hosts,
        tts_service_methods,
    )
    from data.plugins.astrbot_plugin_live_stream_companion.page_config import (
        PageConfigManager,
    )
except ModuleNotFoundError:  # 本地直接跑单测：这些模块都不依赖 AstrBot
    from config_options import (  # type: ignore[no-redef]
        build_external_tts_options,
        build_probe_targets,
        candidate_label,
        describe_external_tts_tool,
        derive_subnet_seeds,
        normalize_host_entries,
        plugin_display_name,
        plugin_identifiers,
        private_ipv4,
        subnet_hosts,
        tts_service_methods,
    )
    from page_config import PageConfigManager  # type: ignore[no-redef]

# unittest 需要能 import 到 tests 包（本地直接跑时补一次包路径）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class _TtsPlugin:
    def __init__(self, display="Voice Hub", plugin_id="astrbot_plugin_voice_hub"):
        self.metadata = SimpleNamespace(name=plugin_id, display_name=display, plugin_id=plugin_id)

    async def text_to_speech(self, text: str):  # noqa: D401 - 方法名即契约
        return f"/tmp/{text}.wav"

    async def render_pcm_wav(self, text: str):
        return {"path": f"/tmp/{text}.wav"}

    async def speech_to_text(self, audio: str):
        return "不该被当成合成方法"

    def _private_helper(self):
        return "私有方法不参与"


class _AsrOnlyPlugin:
    def __init__(self):
        self.metadata = SimpleNamespace(name="astrbot_plugin_asr", display_name="ASR")

    async def transcribe(self, audio: str):
        return "text"


def _tool(handler, name="voice_hub_speak", description="把文本合成语音"):
    return SimpleNamespace(name=name, handler=handler, description=description)


class ExternalTtsOptionTests(unittest.TestCase):
    def test_lists_plugin_and_methods(self):
        plugin = _TtsPlugin()
        options = build_external_tts_options([_tool(plugin.text_to_speech)])
        self.assertEqual(len(options), 1)
        item = options[0]
        self.assertEqual(item["tool"], "voice_hub_speak")
        self.assertEqual(item["plugin"], "astrbot_plugin_voice_hub")
        self.assertEqual(item["plugin_label"], "Voice Hub")
        self.assertEqual(item["methods"], ["render_pcm_wav", "text_to_speech"])
        self.assertIn("Voice Hub", item["label"])

    def test_keeps_tools_without_methods(self):
        """取不到合成方法也要保留（供手动填写），但标记为非 tts_like。"""
        asr = _AsrOnlyPlugin()
        tools = [
            _tool(asr.transcribe, name="asr_transcribe", description="把语音转成文本"),
            _tool(lambda text: text, name="plain_tool", description="普通工具"),
        ]
        options = build_external_tts_options(tools)
        by_tool = {item["tool"]: item for item in options}
        self.assertEqual(len(options), 2)
        self.assertEqual(by_tool["asr_transcribe"]["methods"], [])
        self.assertFalse(by_tool["asr_transcribe"]["tts_like"])
        self.assertFalse(by_tool["plain_tool"]["has_plugin"])

    def test_resolves_plugin_from_closure_handler(self):
        """voice_hub 风格的注册：工具定义在另一个函数里，靠闭包捕获插件实例。"""
        plugin = _TtsPlugin()

        def factory():
            async def tool_handler(text: str = ""):
                return await plugin.text_to_speech(text)

            return tool_handler

        handler = factory()
        item = describe_external_tts_tool(_tool(handler, name="voice_hub_speak"))
        self.assertIsNotNone(item)
        self.assertEqual(item["plugin"], "astrbot_plugin_voice_hub")
        self.assertIn("render_pcm_wav", item["methods"])
        self.assertTrue(item["tts_like"])

    def test_resolves_plugin_from_module_path(self):
        """只有 handler_module_path 时也要能定位插件，并按名字判断像不像 TTS。"""
        tool = _tool(lambda text: text, name="tts_speak", description="朗读文本")
        tool.handler_module_path = "data.plugins.astrbot_plugin_my_voice.main"
        item = describe_external_tts_tool(tool)
        self.assertEqual(item["plugin"], "astrbot_plugin_my_voice")
        self.assertEqual(item["methods"], [])
        self.assertTrue(item["tts_like"])

    def test_sorts_services_before_plain_tools(self):
        plugin = _TtsPlugin()
        tools = [
            _tool(lambda text: text, name="plain_tool", description="普通工具"),
            _tool(plugin.text_to_speech, name="voice_tool"),
        ]
        options = build_external_tts_options(tools)
        self.assertEqual(options[0]["tool"], "voice_tool")

    def test_deduplicates_and_sorts(self):
        plugin = _TtsPlugin(display="Zeta", plugin_id="astrbot_plugin_zeta")
        plugin2 = _TtsPlugin(display="Alpha", plugin_id="astrbot_plugin_alpha")
        tools = [
            _tool(plugin.text_to_speech, name="zeta_speak"),
            _tool(plugin2.text_to_speech, name="alpha_speak"),
            _tool(plugin.text_to_speech, name="zeta_speak"),
        ]
        options = build_external_tts_options(tools)
        self.assertEqual([item["tool"] for item in options], ["alpha_speak", "zeta_speak"])

    def test_helper_functions(self):
        plugin = _TtsPlugin()
        self.assertIn("astrbot_plugin_voice_hub", plugin_identifiers(plugin))
        self.assertEqual(plugin_display_name(plugin), "Voice Hub")
        methods = tts_service_methods(plugin)
        self.assertIn("text_to_speech", methods)
        self.assertIn("render_pcm_wav", methods)
        self.assertNotIn("speech_to_text", methods)
        self.assertNotIn("_private_helper", methods)


class VtsProbeTargetTests(unittest.TestCase):
    def test_private_ipv4_filters(self):
        self.assertEqual(private_ipv4("192.168.5.55"), "192.168.5.55")
        self.assertEqual(private_ipv4("10.0.0.7"), "10.0.0.7")
        self.assertIsNone(private_ipv4("127.0.0.1"))
        self.assertIsNone(private_ipv4("8.8.8.8"))
        self.assertIsNone(private_ipv4("not-an-ip"))

    def test_normalize_host_entries(self):
        self.assertEqual(
            normalize_host_entries("192.168.5.55:8001, 127.0.0.1;host.docker.internal"),
            ["192.168.5.55", "127.0.0.1", "host.docker.internal"],
        )

    def test_build_probe_targets_quick(self):
        plan = build_probe_targets(
            configured_host="192.168.5.55",
            configured_port=8001,
            request_host="192.168.5.88:11451",
        )
        self.assertEqual(plan["hosts"], ["192.168.5.55", "192.168.5.88", "127.0.0.1"])
        self.assertEqual(plan["ports"], [8001])
        self.assertEqual(plan["subnets"], [])

    def test_build_probe_targets_scan_adds_subnets(self):
        plan = build_probe_targets(
            configured_host="192.168.5.55",
            configured_port=8001,
            request_host="192.168.5.88",
            include_scan=True,
        )
        self.assertIn("192.168.5.0/24", plan["subnets"])
        self.assertEqual(len(plan["subnets"]), 1)

    def test_subnet_hosts(self):
        hosts = subnet_hosts("192.168.5.0/24")
        self.assertEqual(len(hosts), 254)
        self.assertIn("192.168.5.55", hosts)
        self.assertNotIn("192.168.5.255", hosts)
        self.assertEqual(subnet_hosts("192.168.5.0/16"), [])
        self.assertEqual(subnet_hosts("bad-value"), [])

    def test_derive_subnet_seeds_ignores_public_and_loopback(self):
        seeds = derive_subnet_seeds(["192.168.5.55", "127.0.0.1", "8.8.8.8"])
        self.assertEqual(seeds, ["192.168.5.0/24"])

    def test_candidate_label(self):
        self.assertEqual(candidate_label("192.168.5.55", 8001, "1.35.10"), "192.168.5.55:8001 · VTS 1.35.10")
        self.assertEqual(candidate_label("127.0.0.1", 8001), "127.0.0.1:8001")


if __name__ == "__main__":
    unittest.main()


class PageConfigManagerTests(unittest.TestCase):
    """拓展页新增的 VTS / L2D 配置项与模板列表解析。"""

    def test_vts_keys_are_editable_and_grouped(self):
        keys = PageConfigManager.editable_keys()
        for key in (
            "vts_host",
            "vts_port",
            "auto_connect",
            "auto_discover",
            "show_status_on_mention",
            "l2d_hotkeys",
            "l2d_max_tags_per_reply",
            "autonomous_l2d_enabled",
            "l2dstudio_exe_path",
        ):
            self.assertIn(key, keys)
        groups = {group["id"]: group for group in PageConfigManager.groups()}
        self.assertIn("connect", groups)
        connect_keys = groups["connect"]["keys"]
        self.assertIn("vts_host", connect_keys)
        self.assertIn("l2d_hotkeys", connect_keys)
        # L2DStudio 路径从 OBS 组移到演出连接组，避免两处重复
        self.assertIn("l2dstudio_exe_path", connect_keys)
        self.assertNotIn("l2dstudio_exe_path", groups["obs"]["keys"])
        self.assertLess(
            list(groups).index("connect"), list(groups).index("live")
        )

    def test_template_list_accepts_json_and_dict(self):
        items = PageConfigManager._coerce_template_list(
            '[{"name": "开心", "hotkey_id": "Smile", "duration": 3}]'
        )
        self.assertEqual(items[0]["name"], "开心")
        self.assertEqual(items[0]["tag"], "开心")
        self.assertEqual(items[0]["hotkey_id"], "Smile")
        self.assertEqual(items[0]["duration"], 3.0)
        self.assertTrue(items[0]["enabled"])
        single = PageConfigManager._coerce_template_list({"name": "难过", "tag": "sad"})
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0]["tag"], "sad")
        self.assertEqual(PageConfigManager._coerce_template_list(""), [])

    def test_template_list_deduplicates_and_validates(self):
        items = PageConfigManager._coerce_template_list(
            '[{"tag": "a", "hotkey_id": "h1"}, {"tag": "a", "hotkey_id": "h2"}, {"not": "valid"}]'
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["hotkey_id"], "h1")
        with self.assertRaises(ValueError):
            PageConfigManager._coerce_template_list("{ not json }")


if __name__ == "__main__":
    unittest.main()
