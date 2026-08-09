"""Twitch 直播字幕支持与自动回应链路的回归测试。"""

import asyncio
import unittest
from collections import deque
from types import SimpleNamespace

from data.plugins.astrbot_plugin_live_stream_companion.bilibili_live import (
    LiveDanmakuEvent,
)
from data.plugins.astrbot_plugin_live_stream_companion.main import VTubeStudioPlugin
from data.plugins.astrbot_plugin_live_stream_companion.twitch_live import (
    TwitchIrcClient,
    _parse_tags,
    _unescape_tag,
)


class TwitchIrcParsingTests(unittest.TestCase):
    def test_parse_normal_danmaku(self):
        line = (
            "@badge-info=;badges=;color=#FF0000;display-name=SomeUser;emotes=;"
            "first-msg=0;flags=;id=abc123;mod=0;room-id=123;subscriber=0;"
            "tmi-sent-ts=1700000000000;turbo=0;user-id=98765;user-type= "
            ":someuser!someuser@someuser.tmi.twitch.tv PRIVMSG #mychannel :hello :world how are you"
        )
        client = TwitchIrcClient("mychannel", None)
        event = client._parse_line(line)
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "danmaku")
        self.assertEqual(event.username, "SomeUser")
        self.assertEqual(event.content, "hello :world how are you")
        self.assertEqual(event.raw["platform"], "twitch")
        self.assertEqual(event.raw["channel"], "mychannel")
        self.assertEqual(event.raw["user_id"], "98765")
        self.assertEqual(event.raw["login"], "someuser")

    def test_parse_display_name_fallback_to_login(self):
        line = (
            "@display-name=;user-id=1 "
            ":someuser!someuser@someuser.tmi.twitch.tv PRIVMSG #c :hi"
        )
        client = TwitchIrcClient("c", None)
        event = client._parse_line(line)
        self.assertIsNotNone(event)
        self.assertEqual(event.username, "someuser")

    def test_parse_empty_message_returns_none(self):
        line = (
            "@display-name=U;user-id=1 "
            ":u!u@u.tmi.twitch.tv PRIVMSG #c :   "
        )
        client = TwitchIrcClient("c", None)
        self.assertIsNone(client._parse_line(line))

    def test_non_privmsg_lines_return_none(self):
        client = TwitchIrcClient("c", None)
        self.assertIsNone(client._parse_line("PING :tmi.twitch.tv"))
        self.assertIsNone(client._parse_line(":tmi.twitch.tv 001 justinfan12345 :Welcome"))
        self.assertIsNone(client._parse_line(":tmi.twitch.tv 366 justinfan12345 #c :End of /NAMES list"))
        self.assertIsNone(client._parse_line(""))

    def test_parse_tags_and_unescape(self):
        tags = _parse_tags("display-name=Hello\\sWorld;user-id=42;emotes=")
        self.assertEqual(tags["user-id"], "42")
        self.assertEqual(tags["emotes"], "")
        self.assertEqual(_unescape_tag("Hello\\sWorld"), "Hello World")
        self.assertEqual(_unescape_tag("a\\:b"), "a;b")

    def test_channel_normalization(self):
        self.assertEqual(TwitchIrcClient("#MyChannel", None).channel, "mychannel")
        self.assertEqual(TwitchIrcClient("  TwitchTV  ", None).channel, "twitchtv")


class SubtitleScopeTwitchTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, **config):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = config
        plugin._twitch_live_task = None
        plugin._twitch_client = None
        return plugin

    def test_scope_twitch_live_source_allowed(self):
        plugin = self._plugin(subtitle_scope="twitch_live")
        self.assertTrue(plugin._source_should_push_subtitle("twitch_live"))
        self.assertTrue(plugin._source_should_push_subtitle("manual"))
        self.assertTrue(plugin._source_should_push_subtitle("preview"))
        self.assertFalse(plugin._source_should_push_subtitle("bili_live"))
        self.assertFalse(plugin._source_should_push_subtitle(""))
        self.assertFalse(plugin._source_should_push_subtitle("external"))

    def test_scope_bili_live_source_rejects_twitch(self):
        plugin = self._plugin(subtitle_scope="bili_live")
        self.assertFalse(plugin._source_should_push_subtitle("twitch_live"))
        self.assertTrue(plugin._source_should_push_subtitle("bili_live"))
        self.assertTrue(plugin._source_should_push_subtitle("manual"))
        self.assertTrue(plugin._source_should_push_subtitle("preview"))

    def test_scope_all_pushes_everything(self):
        plugin = self._plugin(subtitle_scope="all")
        self.assertTrue(plugin._source_should_push_subtitle("twitch_live"))
        self.assertTrue(plugin._source_should_push_subtitle("bili_live"))
        self.assertTrue(plugin._source_should_push_subtitle(""))
        self.assertTrue(plugin._source_should_push_subtitle("anything"))

    def test_scope_unknown_falls_back_to_all(self):
        plugin = self._plugin(subtitle_scope="weird_value")
        self.assertEqual(plugin._subtitle_scope(), "all")
        self.assertTrue(plugin._source_should_push_subtitle("twitch_live"))

    def test_scope_twitch_live_together_companion_checks_twitch_running(self):
        plugin = self._plugin(subtitle_scope="twitch_live")
        plugin._twitch_live_task = SimpleNamespace(done=lambda: False)
        self.assertTrue(plugin._source_should_push_subtitle("together_companion"))
        plugin._twitch_live_task = SimpleNamespace(done=lambda: True)
        self.assertFalse(plugin._source_should_push_subtitle("together_companion"))

    def test_event_should_push_subtitle_twitch_scope(self):
        plugin = self._plugin(subtitle_scope="twitch_live")

        class _Event:
            def __init__(self, extra):
                self._extra = extra

            def get_extra(self, key):
                return self._extra.get(key)

        self.assertTrue(
            plugin._event_should_push_subtitle(_Event({"twitch_live_auto_reply": True}))
        )
        self.assertFalse(
            plugin._event_should_push_subtitle(_Event({"bili_live_auto_reply": True}))
        )
        self.assertFalse(plugin._event_should_push_subtitle(_Event({})))

    def test_event_should_push_subtitle_bili_scope_still_works(self):
        plugin = self._plugin(subtitle_scope="bili_live")

        class _Event:
            def get_extra(self, key):
                return key == "bili_live_auto_reply"

        self.assertTrue(plugin._event_should_push_subtitle(_Event()))
        self.assertFalse(
            plugin._event_should_push_subtitle(
                SimpleNamespace(get_extra=lambda key: key == "twitch_live_auto_reply")
            )
        )


class TwitchAutoReplyWorkerTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, **config):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = config
        plugin._twitch_last_auto_reply_at = 0.0
        plugin._twitch_pending_reply_events = deque()
        plugin._twitch_auto_reply_task = None
        plugin._twitch_auto_reply_minute_marks = deque()
        plugin._twitch_auto_reply_history = deque()
        return plugin

    async def test_min_events_not_reached_keeps_events(self):
        plugin = self._plugin(
            twitch_auto_reply_min_events=2,
            twitch_auto_reply_cooldown_seconds=1,
        )
        plugin._twitch_pending_reply_events.append(
            LiveDanmakuEvent("danmaku", "viewer", "first")
        )
        scheduled = []
        plugin._schedule_twitch_auto_reply = lambda: scheduled.append(True)

        await plugin._twitch_auto_reply_worker()
        await asyncio.sleep(0)

        self.assertEqual(scheduled, [])
        self.assertEqual(len(plugin._twitch_pending_reply_events), 1)

    async def test_worker_replies_when_ready(self):
        plugin = self._plugin(
            twitch_auto_reply_min_events=1,
            twitch_auto_reply_cooldown_seconds=1,
            twitch_auto_reply_max_per_minute=0,
            twitch_auto_reply_air_guard_enabled=False,
        )
        plugin._twitch_pending_reply_events.append(
            LiveDanmakuEvent("danmaku", "viewer", "hello there")
        )
        replied = []

        async def fake_reply(events):
            replied.append(events)

        plugin._reply_to_twitch_live_events = fake_reply
        plugin._schedule_twitch_auto_reply = lambda: None

        await plugin._twitch_auto_reply_worker()
        await asyncio.sleep(0)

        self.assertEqual(len(replied), 1)
        self.assertEqual(replied[0][0].content, "hello there")

    async def test_cooldown_blocks_reply_and_keeps_events(self):
        plugin = self._plugin(
            twitch_auto_reply_min_events=1,
            twitch_auto_reply_cooldown_seconds=60,
        )
        plugin._twitch_last_auto_reply_at = 9999999999.0  # 刚回复过
        plugin._twitch_pending_reply_events.append(
            LiveDanmakuEvent("danmaku", "viewer", "hello")
        )
        replied = []
        plugin._reply_to_twitch_live_events = lambda events: replied.append(events)
        plugin._schedule_twitch_auto_reply = lambda: None

        await plugin._twitch_auto_reply_worker()
        await asyncio.sleep(0)

        self.assertEqual(replied, [])
        self.assertEqual(len(plugin._twitch_pending_reply_events), 1)

    def test_air_guard_silent_on_chitchat(self):
        plugin = self._plugin(twitch_auto_reply_air_guard_enabled=True)
        decision = plugin._twitch_air_guard_local_decision(
            [LiveDanmakuEvent("danmaku", "viewer", "哈哈")]
        )
        self.assertFalse(decision["reply"])

    def test_air_guard_reply_on_question(self):
        plugin = self._plugin(twitch_auto_reply_air_guard_enabled=True)
        decision = plugin._twitch_air_guard_local_decision(
            [LiveDanmakuEvent("danmaku", "viewer", "今天玩什么游戏？")]
        )
        self.assertTrue(decision["reply"])

    def test_air_guard_disabled_replies(self):
        plugin = self._plugin(twitch_auto_reply_air_guard_enabled=False)

        async def run():
            return await plugin._should_reply_to_twitch_events(
                [LiveDanmakuEvent("danmaku", "viewer", "哈哈")]
            )

        reply, reason = asyncio.get_event_loop().run_until_complete(run())
        self.assertTrue(reply)
        self.assertEqual(reason, "air_guard_disabled")


class TwitchReplyTtsTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, **config):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = config
        plugin._twitch_last_auto_reply_at = 0.0
        plugin._twitch_auto_reply_minute_marks = deque()
        plugin._twitch_auto_reply_history = deque()
        return plugin

    async def test_tts_disabled_sends_plain(self):
        plugin = self._plugin(
            twitch_auto_reply_tts_enabled=False,
            bili_live_tts_local_playback_enabled=False,
        )
        sent = []
        pushed = []
        recorded = []
        plugin.context = SimpleNamespace()

        async def fake_send(sid, chain):
            sent.append((sid, chain))

        async def fake_push(text, source):
            pushed.append((text, source))

        plugin.context.send_message = fake_send
        plugin._push_subtitle = fake_push
        plugin._record_twitch_auto_reply_sent = lambda events, text: recorded.append(text)

        await plugin._send_twitch_reply("session1", "你好呀", [])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "session1")
        self.assertEqual(pushed[0][1], "twitch_live")
        self.assertEqual(recorded, ["你好呀"])

    async def test_tts_failure_falls_back_to_plain(self):
        plugin = self._plugin(
            twitch_auto_reply_tts_enabled=True,
            bili_live_tts_local_playback_enabled=False,
        )
        sent = []
        plugin.context = SimpleNamespace()

        async def fake_send(sid, chain):
            sent.append(chain)

        plugin.context.send_message = fake_send

        async def fake_push(text, source):
            return None

        plugin._push_subtitle = fake_push
        plugin._record_twitch_auto_reply_sent = lambda events, text: None

        async def fake_tts_payload(*args, **kwargs):
            return {}

        plugin._build_bili_live_tts_payload = fake_tts_payload

        await plugin._send_twitch_reply("session1", "你好", [])
        self.assertEqual(len(sent), 1)
        from astrbot.api.message_components import Plain

        chain = sent[0].chain if hasattr(sent[0], "chain") else list(sent[0])
        self.assertTrue(any(isinstance(c, Plain) for c in chain))

    async def test_tts_success_uses_record_chain(self):
        plugin = self._plugin(
            twitch_auto_reply_tts_enabled=True,
            bili_live_tts_local_playback_enabled=False,
        )
        sent = []
        plugin.context = SimpleNamespace()

        async def fake_send(sid, chain):
            sent.append(chain)

        plugin.context.send_message = fake_send

        async def fake_push(text, source):
            return None

        plugin._push_subtitle = fake_push
        plugin._record_twitch_auto_reply_sent = lambda events, text: None

        async def fake_mouth(chain):
            return None

        plugin._start_bili_live_mouth_sync_for_chain = fake_mouth

        from astrbot.api.message_components import Record

        record = Record(file="dummy.wav")

        async def fake_tts_payload(*args, **kwargs):
            return {"chain": [record], "audio_path": "dummy.wav"}

        plugin._build_bili_live_tts_payload = fake_tts_payload

        await plugin._send_twitch_reply("session1", "你好", [])
        self.assertEqual(len(sent), 1)
        chain = sent[0].chain if hasattr(sent[0], "chain") else list(sent[0])
        self.assertTrue(any(isinstance(c, Record) for c in chain))


if __name__ == "__main__":
    unittest.main()
