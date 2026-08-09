"""陪伴插件外部主动能力连接自愈（6.0.4.1）的回归测试。"""

import asyncio
import unittest

from data.plugins.astrbot_plugin_live_stream_companion.main import VTubeStudioPlugin


class _FakeApi:
    def __init__(self, abilities):
        self._abilities = abilities

    def list_proactive_abilities(self):
        return self._abilities


class CompanionLinkHealingTests(unittest.TestCase):
    def _host(self, api):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin._private_companion_extension_api = lambda: api
        return plugin

    def test_all_abilities_registered(self):
        api = _FakeApi(
            [
                {"name": "live_stream_start", "available": True},
                {"name": "live_stream_stop", "available": True},
                {"name": "other", "available": False},
            ]
        )
        self.assertTrue(self._host(api)._private_companion_abilities_registered())

    def test_start_missing_triggers_healing(self):
        api = _FakeApi([{"name": "live_stream_stop", "available": True}])
        self.assertFalse(self._host(api)._private_companion_abilities_registered())

    def test_stop_not_available_triggers_healing(self):
        api = _FakeApi(
            [
                {"name": "live_stream_start", "available": True},
                {"name": "live_stream_stop", "available": False},
            ]
        )
        self.assertFalse(self._host(api)._private_companion_abilities_registered())

    def test_api_unavailable_triggers_healing(self):
        self.assertFalse(self._host(None)._private_companion_abilities_registered())

    def test_non_list_response_triggers_healing(self):
        self.assertFalse(self._host(_FakeApi(None))._private_companion_abilities_registered())


class CompanionLinkHealingLoopTests(unittest.TestCase):
    """自愈循环行为：陪伴插件缺失时快速结束，不进入无限周期任务。"""

    def _run_with_fast_sleep(self, coro):
        """用瞬时 sleep 运行协程，避免真实等待；返回 (结果, sleep 调用次数)。"""
        original_sleep = asyncio.sleep
        calls = []

        async def _fast_sleep(_seconds):
            calls.append(_seconds)

        asyncio.sleep = _fast_sleep
        try:
            asyncio.run(coro())
        finally:
            asyncio.sleep = original_sleep
        return calls

    def test_missing_companion_exits_loop_quickly(self):
        """没装陪伴插件：快速窗口耗尽后协程应立即返回，不进入周期循环空转。"""
        plugin = object.__new__(VTubeStudioPlugin)
        plugin._private_companion_extension_api = lambda: None
        plugin._register_private_companion_proactive_abilities = lambda: False

        async def run():
            await plugin._register_private_companion_proactive_abilities_with_retry()

        calls = self._run_with_fast_sleep(run)
        # 只应经历快速窗口（最多 12 次 sleep），一旦进入周期循环会无限 sleep(60)。
        self.assertLessEqual(len(calls), 12)

    def test_installed_companion_keeps_healing_loop_alive(self):
        """陪伴插件在但能力缺失：应进入周期循环并持续校验（sleep 超过快速窗口数）。"""
        plugin = object.__new__(VTubeStudioPlugin)
        plugin._private_companion_extension_api = lambda: _FakeApi([])
        plugin._register_private_companion_proactive_abilities = lambda: False

        async def run():
            task = asyncio.create_task(
                plugin._register_private_companion_proactive_abilities_with_retry()
            )
            # 给周期循环跑 15 个周期（快速窗口 12 + 周期若干），然后取消
            for _ in range(27):
                await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        calls = self._run_with_fast_sleep(run)
        # 进入周期循环后应远超 12 次（快速窗口 + 周期校验）
        self.assertGreater(len(calls), 12)
