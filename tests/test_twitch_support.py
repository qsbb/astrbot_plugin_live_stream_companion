# -*- coding: utf-8 -*-
import asyncio
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from data.plugins.astrbot_plugin_live_stream_companion.bilibili_live import (
    LiveDanmakuEvent,
)
from data.plugins.astrbot_plugin_live_stream_companion.main import VTubeStudioPlugin
from data.plugins.astrbot_plugin_live_stream_companion.page_api import (
    LiveStreamCompanionPageApi,
)
from data.plugins.astrbot_plugin_live_stream_companion.twitch_live import (
    TwitchIrcClient,
    _parse_tags,
    normalize_twitch_channel,
)


class TwitchIrcTests(unittest.TestCase):
    def test_channel_name_and_url_normalization(self):
        self.assertEqual(normalize_twitch_channel("#Some_Channel"), "some_channel")
        self.assertEqual(
            normalize_twitch_channel("https://www.twitch.tv/Some_Channel/videos"),
            "some_channel",
        )
        self.assertEqual(
            normalize_twitch_channel("twitch.tv/Some_Channel?ref=test"),
            "some_channel",
        )
        with self.assertRaises(ValueError):
            normalize_twitch_channel("https://example.com/not-twitch")

    def test_irc_tags_decode_all_supported_escapes(self):
        tags = _parse_tags(
            r"display-name=Hello\sWorld;note=a\:b\\c\nnext\rline;empty="
        )
        self.assertEqual(tags["display-name"], "Hello World")
        self.assertEqual(tags["note"], "a;b\\c\nnext\rline")
        self.assertEqual(tags["empty"], "")

    def test_privmsg_and_action_parsing(self):
        client = TwitchIrcClient("channel", None)
        event = client._parse_line(
            "@display-name=Some\\sUser;user-id=42 "
            ":someuser!someuser@someuser.tmi.twitch.tv "
            "PRIVMSG #channel :\x01ACTION waves hello\x01"
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.username, "Some User")
        self.assertEqual(event.content, "waves hello")
        self.assertTrue(event.raw["is_action"])
        self.assertEqual(event.raw["platform"], "twitch")

    def test_reconnect_warning_is_rate_limited(self):
        client = TwitchIrcClient("channel", None, warning_interval=60)
        with (
            patch(
                "data.plugins.astrbot_plugin_live_stream_companion.twitch_live.time.monotonic",
                side_effect=[100.0, 101.0],
            ),
            patch(
                "data.plugins.astrbot_plugin_live_stream_companion.twitch_live.logger"
            ) as mocked_logger,
        ):
            client._log_connection_failure(ConnectionError("offline"), 5)
            client._log_connection_failure(ConnectionError("offline"), 10)

        mocked_logger.warning.assert_called_once()
        mocked_logger.debug.assert_called_once()


class TwitchSubtitleScopeTests(unittest.TestCase):
    @staticmethod
    def _plugin(scope: str):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = {"subtitle_scope": scope}
        plugin._is_bili_live_running = lambda: False
        plugin._is_twitch_live_running = lambda: True
        return plugin

    def test_live_scope_accepts_both_live_sources(self):
        plugin = self._plugin("live")
        self.assertTrue(plugin._source_should_push_subtitle("bili_live"))
        self.assertTrue(plugin._source_should_push_subtitle("twitch_live"))
        self.assertTrue(plugin._source_should_push_subtitle("manual"))
        self.assertFalse(plugin._source_should_push_subtitle(""))
        self.assertTrue(plugin._source_should_push_subtitle("together_companion"))

    def test_live_scope_accepts_both_event_markers(self):
        plugin = self._plugin("live")
        bili = SimpleNamespace(
            get_extra=lambda key: key == "bili_live_auto_reply"
        )
        twitch = SimpleNamespace(
            get_extra=lambda key: key == "twitch_live_auto_reply"
        )
        normal = SimpleNamespace(get_extra=lambda _key: False)
        self.assertTrue(plugin._event_should_push_subtitle(bili))
        self.assertTrue(plugin._event_should_push_subtitle(twitch))
        self.assertFalse(plugin._event_should_push_subtitle(normal))

    def test_unknown_scope_falls_back_to_bili_only(self):
        plugin = self._plugin("invalid")
        self.assertEqual(plugin._subtitle_scope(), "bili_live")
        self.assertTrue(plugin._source_should_push_subtitle("bili_live"))
        self.assertFalse(plugin._source_should_push_subtitle("twitch_live"))


class TwitchAutoReplyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _plugin(**config):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = config
        plugin._twitch_last_auto_reply_at = 0.0
        plugin._twitch_pending_reply_events = deque()
        plugin._twitch_auto_reply_task = None
        plugin._twitch_auto_reply_minute_marks = deque()
        plugin._twitch_auto_reply_history = deque()
        return plugin

    async def test_worker_drains_only_configured_max_events(self):
        plugin = self._plugin(
            twitch_auto_reply_enabled=True,
            twitch_auto_reply_min_events=1,
            twitch_auto_reply_max_events=2,
            twitch_auto_reply_cooldown_seconds=1,
            twitch_auto_reply_max_per_minute=0,
        )
        for index in range(3):
            plugin._twitch_pending_reply_events.append(
                LiveDanmakuEvent("danmaku", "viewer", f"message-{index}")
            )
        plugin._wait_twitch_reply_window = AsyncMock()
        plugin._should_reply_to_twitch_events = AsyncMock(
            return_value=(True, "test")
        )
        batches = []

        async def reply(events):
            batches.append(events)
            return True

        scheduled = []
        plugin._reply_to_twitch_live_events = reply
        plugin._schedule_twitch_auto_reply = lambda: scheduled.append(True)

        await plugin._twitch_auto_reply_worker()
        await asyncio.sleep(0)

        self.assertEqual([len(batch) for batch in batches], [2])
        self.assertEqual(len(plugin._twitch_pending_reply_events), 1)
        self.assertEqual(scheduled, [True])

    async def test_send_failure_does_not_consume_reply_quota(self):
        plugin = self._plugin(twitch_auto_reply_tts_enabled=False)
        plugin.context = SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("send failed"))
        )
        plugin._push_subtitle = AsyncMock()
        plugin._record_twitch_auto_reply_sent = Mock()

        sent = await plugin._send_twitch_reply("session", "hello", [])

        self.assertFalse(sent)
        plugin._record_twitch_auto_reply_sent.assert_not_called()
        self.assertEqual(plugin._twitch_last_auto_reply_at, 0.0)
        self.assertEqual(len(plugin._twitch_auto_reply_minute_marks), 0)

    async def test_tts_payload_and_local_playback_are_scheduled(self):
        plugin = self._plugin(twitch_auto_reply_tts_enabled=True)
        plugin.context = SimpleNamespace(send_message=AsyncMock())
        plugin._build_bili_live_tts_payload = AsyncMock(
            return_value={"chain": [], "audio_path": "reply.wav"}
        )
        plugin._push_subtitle = AsyncMock()
        plugin._start_bili_live_mouth_sync_for_chain = AsyncMock()
        plugin._schedule_bili_live_tts_local_playback = Mock()
        plugin._record_twitch_auto_reply_sent = Mock()

        sent = await plugin._send_twitch_reply("session", "hello", [])

        self.assertTrue(sent)
        plugin._schedule_bili_live_tts_local_playback.assert_called_once_with(
            "reply.wav"
        )
        plugin._build_bili_live_tts_payload.assert_awaited_once()
        plugin._record_twitch_auto_reply_sent.assert_called_once()

    async def test_stop_cancels_pending_auto_reply(self):
        plugin = self._plugin()
        plugin._twitch_client = None
        plugin._twitch_live_task = None
        plugin._twitch_pending_reply_events.append(
            LiveDanmakuEvent("danmaku", "viewer", "pending")
        )
        blocker = asyncio.create_task(asyncio.Event().wait())
        plugin._twitch_auto_reply_task = blocker

        await plugin._stop_twitch_live(log=False)

        self.assertTrue(blocker.cancelled())
        self.assertIsNone(plugin._twitch_auto_reply_task)
        self.assertEqual(len(plugin._twitch_pending_reply_events), 0)


class TwitchManagementPayloadTests(unittest.TestCase):
    def test_recent_event_payload_contains_platform(self):
        plugin = object.__new__(VTubeStudioPlugin)
        api = LiveStreamCompanionPageApi(plugin)
        event = LiveDanmakuEvent(
            "danmaku",
            "viewer",
            "hello",
            raw={"platform": "twitch"},
        )

        payload = api._event_payload(event)

        self.assertEqual(payload["platform"], "twitch")


if __name__ == "__main__":
    unittest.main()
