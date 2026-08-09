# -*- coding: utf-8 -*-
"""Regression tests for companion capability healing and platform routing."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from data.plugins.astrbot_plugin_live_stream_companion.main import VTubeStudioPlugin


class _FakeApi:
    def __init__(self, abilities=None):
        self.abilities = abilities
        self.registered = []

    def list_proactive_abilities(self):
        return self.abilities

    def register_proactive_ability(self, spec):
        self.registered.append(spec)
        return True


class CompanionLinkHealingTests(unittest.TestCase):
    @staticmethod
    def _host(api):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin._private_companion_extension_api = lambda: api
        return plugin

    def test_both_abilities_must_have_live_executors(self):
        api = _FakeApi(
            [
                {"name": "live_stream_start", "available": True},
                {"name": "live_stream_stop", "available": True},
                {"name": "other", "available": False},
            ]
        )
        self.assertTrue(self._host(api)._private_companion_abilities_registered())

        api.abilities[1]["available"] = False
        self.assertFalse(self._host(api)._private_companion_abilities_registered())

    def test_missing_or_invalid_api_triggers_healing(self):
        self.assertFalse(self._host(None)._private_companion_abilities_registered())
        self.assertFalse(
            self._host(_FakeApi(abilities=None))._private_companion_abilities_registered()
        )

    def test_registered_specs_expose_typed_platform_controls(self):
        api = _FakeApi([])
        plugin = self._host(api)

        self.assertTrue(plugin._register_private_companion_proactive_abilities())

        specs = {item["name"]: item for item in api.registered}
        start = specs["live_stream_start"]
        stop = specs["live_stream_stop"]
        self.assertEqual(start["default_config"]["platform"], "auto")
        self.assertEqual(start["config_schema"]["platform"]["type"], "select")
        self.assertEqual(start["config_schema"]["start_listener"]["type"], "bool")
        self.assertEqual(start["config_schema"]["wait_seconds"]["type"], "number")
        self.assertEqual(stop["config_schema"]["platform"]["type"], "select")


class CompanionLinkHealingLoopTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _plugin(api, register_result=False):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin._private_companion_extension_api = lambda: api
        plugin._register_private_companion_proactive_abilities = Mock(
            return_value=register_result
        )
        return plugin

    async def test_missing_companion_exits_after_fast_window(self):
        plugin = self._plugin(None)
        sleep = AsyncMock()
        with patch(
            "data.plugins.astrbot_plugin_live_stream_companion.main.asyncio.sleep",
            sleep,
        ):
            await plugin._register_private_companion_proactive_abilities_with_retry()

        self.assertLessEqual(sleep.await_count, 12)

    async def test_installed_companion_uses_capped_backoff(self):
        plugin = self._plugin(_FakeApi([]), register_result=True)
        plugin._register_private_companion_proactive_abilities = Mock(
            side_effect=[True, False, False, False, False]
        )
        delays = []

        async def controlled_sleep(delay):
            delays.append(delay)
            if len(delays) >= 5:
                raise asyncio.CancelledError

        with patch(
            "data.plugins.astrbot_plugin_live_stream_companion.main.asyncio.sleep",
            side_effect=controlled_sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await plugin._register_private_companion_proactive_abilities_with_retry()

        self.assertEqual(delays, [60.0, 120.0, 240.0, 480.0, 600.0])


class ProactivePlatformRoutingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _plugin(**config):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = config
        plugin._is_twitch_live_running = lambda: False
        plugin._is_bili_live_running = lambda: False
        plugin._is_twitch_enabled = lambda: bool(config.get("twitch_enabled"))
        plugin._get_twitch_channel = lambda: str(config.get("twitch_channel") or "")
        return plugin

    def test_auto_prefers_current_listener_before_configured_twitch(self):
        plugin = self._plugin(twitch_enabled=True, twitch_channel="channel")
        plugin._is_bili_live_running = lambda: True

        self.assertEqual(plugin._resolve_proactive_live_platform({}), "bili")
        self.assertEqual(
            plugin._resolve_proactive_live_platform({"platform": "twitch"}),
            "twitch",
        )

    def test_auto_uses_configured_twitch_when_idle(self):
        plugin = self._plugin(twitch_enabled=True, twitch_channel="channel")
        self.assertEqual(plugin._resolve_proactive_live_platform({}), "twitch")

    async def test_twitch_does_not_query_bilibili_area(self):
        plugin = self._plugin(twitch_enabled=True, twitch_channel="channel")
        plugin._find_bili_area = AsyncMock()

        area = await plugin._resolve_proactive_live_area(
            {"area_query": "86"},
            platform="twitch",
        )

        self.assertIsNone(area)
        plugin._find_bili_area.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
