import asyncio
import datetime
import json
import time
import unittest
from collections import deque
from types import SimpleNamespace

from data.plugins.astrbot_plugin_live_stream_companion.bilibili_live import (
    LiveDanmakuEvent,
)
from data.plugins.astrbot_plugin_live_stream_companion.main import VTubeStudioPlugin
from data.plugins.astrbot_plugin_live_stream_companion.main import LiveStreamCompanionExtensionAPI
from data.plugins.astrbot_plugin_live_stream_companion.vts_parameter_scheduler import (
    VTSParameterScheduler,
)
from data.plugins.astrbot_plugin_live_stream_companion.vts_client import (
    VTSClient,
    VTSRealtimeClient,
    VTSResponseError,
)


class _ActivityAPI:
    def __init__(self) -> None:
        self.requested_dates: list[str] = []

    def activity_overview(self, date: str = "") -> str:
        self.requested_dates.append(date)
        return f"activity:{date or 'today'}"


class LiveReplyRegressionTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, **config):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = config
        plugin._bili_last_auto_reply_at = 0.0
        plugin._bili_pending_reply_events = deque()
        plugin._bili_processing_support_event_ids = set()
        plugin._bili_acknowledged_support_event_ids = set()
        return plugin

    async def test_minimum_event_wait_does_not_reschedule_itself(self):
        plugin = self._plugin(
            bili_live_auto_reply_min_events=2,
            bili_live_auto_reply_cooldown_seconds=1,
        )
        plugin._bili_pending_reply_events.append(
            LiveDanmakuEvent("danmaku", "viewer", "first")
        )
        scheduled = []
        plugin._schedule_bili_auto_reply = lambda: scheduled.append(True)

        await plugin._bili_auto_reply_worker()
        await asyncio.sleep(0)

        self.assertEqual(scheduled, [])
        self.assertEqual(len(plugin._bili_pending_reply_events), 1)
    async def test_events_arriving_during_reply_are_rescheduled(self):
        plugin = self._plugin(
            bili_live_auto_reply_min_events=1,
            bili_live_auto_reply_cooldown_seconds=1,
        )
        plugin._bili_pending_reply_events.append(
            LiveDanmakuEvent("danmaku", "viewer", "first")
        )
        scheduled = []

        async def reply(_events):
            plugin._bili_pending_reply_events.append(
                LiveDanmakuEvent("danmaku", "viewer", "second")
            )

        async def fallback(_events, **_kwargs):
            return True

        plugin._reply_to_bili_live_events = reply
        plugin._send_unacknowledged_bili_support_fallback = fallback
        plugin._schedule_bili_auto_reply = lambda: scheduled.append(True)

        await plugin._bili_auto_reply_worker()
        await asyncio.sleep(0)

        self.assertEqual(scheduled, [True])
        self.assertEqual(len(plugin._bili_pending_reply_events), 1)

    def test_each_super_chat_sender_must_be_thanked_in_the_same_clause(self):
        plugin = self._plugin()
        events = [
            LiveDanmakuEvent("super_chat", "Alice", "first", amount=30),
            LiveDanmakuEvent("super_chat", "Bob", "second", amount=50),
        ]

        result = plugin._ensure_bili_support_acknowledgement(
            "谢谢Alice的SC，Bob的问题我接着回答。", events
        )

        self.assertIn("谢谢Bob的50元SC", result)
        self.assertEqual(result.count("谢谢Alice"), 1)

    def test_group_thanks_are_not_duplicated(self):
        plugin = self._plugin()
        events = [
            LiveDanmakuEvent("super_chat", "Alice", "first"),
            LiveDanmakuEvent("super_chat", "Bob", "second"),
        ]

        result = plugin._ensure_bili_support_acknowledgement(
            "谢谢Alice和Bob的SC！", events
        )

        self.assertEqual(result, "谢谢Alice和Bob的SC！")

    def test_recent_activity_reads_three_calendar_days(self):
        plugin = self._plugin(bilibili_ai_memory_integration_enabled=True)
        api = _ActivityAPI()
        plugin._get_bilibili_ai_bot_api = lambda: api

        context = plugin._build_bilibili_ai_self_activity_context(
            [LiveDanmakuEvent("danmaku", "viewer", "你最近看了什么？")]
        )

        today = datetime.date.today()
        expected = [
            (today - datetime.timedelta(days=offset)).isoformat()
            for offset in range(3)
        ]
        self.assertEqual(api.requested_dates, expected)
        self.assertIn("活动范围：最近三天", context)
        for date in expected:
            self.assertIn(date, context)

    def test_vts_command_fallback_keeps_current_chat_context(self):
        plugin = self._plugin()
        event = SimpleNamespace(message_str="/vts_auth")
        request = SimpleNamespace(system_prompt="base")

        plugin._inject_vts_command_fallback_instruction(event, request)

        self.assertIn("本次 AstrBot 会话", request.system_prompt)
        self.assertIn("不要声称用户必须去 QQ", request.system_prompt)

    def test_vts_command_fallback_ignores_normal_chat(self):
        plugin = self._plugin()
        event = SimpleNamespace(message_str="怎么认证 VTS？")
        request = SimpleNamespace(system_prompt="base")

        plugin._inject_vts_command_fallback_instruction(event, request)

        self.assertEqual(request.system_prompt, "base")


class ExternalCompanionAPITests(unittest.IsolatedAsyncioTestCase):
    def test_together_subtitle_is_allowed_while_bili_live_is_running(self):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = {"subtitle_scope": "bili_live"}
        plugin._is_bili_live_running = lambda: True

        self.assertTrue(plugin._source_should_push_subtitle("together_companion"))

    async def test_external_subtitle_uses_existing_subtitle_pipeline(self):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = {"subtitle_enabled": True}
        received = []

        async def push(text, *, source=""):
            received.append((text, source))

        plugin._push_subtitle = push
        api = LiveStreamCompanionExtensionAPI(plugin)

        pushed = await api.push_external_subtitle("一起看吧", source="together_companion")

        self.assertTrue(pushed)
        self.assertEqual([("一起看吧", "together_companion")], received)

    async def test_external_mouth_sync_tasks_can_be_stopped_by_source(self):
        plugin = object.__new__(VTubeStudioPlugin)
        blocker = asyncio.create_task(asyncio.Event().wait())
        plugin._external_mouth_sync_tasks = {"together:room": {blocker}}
        api = LiveStreamCompanionExtensionAPI(plugin)

        stopped = await api.stop_external_mouth_sync(source="together:room")

        self.assertEqual(1, stopped)
        self.assertTrue(blocker.cancelling())
        with self.assertRaises(asyncio.CancelledError):
            await blocker


class SoullinkIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, **config):
        plugin = object.__new__(VTubeStudioPlugin)
        plugin.config = config
        plugin._soullink_tasks = set()
        plugin._soullink_last_vts_parameters = []
        return plugin

    def test_prompt_instruction_is_optional(self):
        disabled = self._plugin(soullink_enabled=False)
        request = SimpleNamespace(system_prompt="base")
        disabled._inject_soullink_prompt_instruction(request)
        self.assertEqual(request.system_prompt, "base")

        enabled = self._plugin(
            soullink_enabled=True,
            soullink_prompt_intent_enabled=True,
        )
        enabled._inject_soullink_prompt_instruction(request)
        self.assertIn("Soullink 实时表演", request.system_prompt)
        self.assertIn("不要为了触发表演改变正文", request.system_prompt)
        self.assertIn("不要把用户的情绪", request.system_prompt)
        self.assertIn("别难过", request.system_prompt)

    def test_soullink_tag_is_parsed_and_removed(self):
        plugin = self._plugin(soullink_enabled=True)
        intent, cleaned = plugin._parse_soullink_intent(
            '今天真的很开心！\n<soullink>{"emotion":"happy",'
            '"intensity":0.82,"vad":{"valence":0.75,'
            '"arousal":0.5,"dominance":0.3}}</soullink>'
        )

        self.assertEqual(cleaned, "今天真的很开心！")
        self.assertEqual(intent["emotion"], "happy")
        self.assertEqual(intent["intensity"], 0.82)
        self.assertEqual(intent["vad"]["valence"], 0.75)

    def test_soullink_frame_maps_to_vts_inputs(self):
        plugin = self._plugin(soullink_enabled=True, soullink_vts_mapping="{}")
        parameters = plugin._map_soullink_frame_to_vts(
            {
                "facs": {
                    "headX": 0.5,
                    "headY": -0.25,
                    "headZ": 0.1,
                    "gazeX": 0.2,
                    "gazeY": -0.1,
                    "eyeOpen": 0.9,
                    "mouthSmile": 0.6,
                    "browInnerUp": 0.4,
                    "browOuterUp": 0.2,
                    "browDown": 0.1,
                }
            }
        )
        values = {item["id"]: item["value"] for item in parameters}

        self.assertEqual(values["FaceAngleX"], 18.0)
        self.assertEqual(values["FaceAngleY"], -8.0)
        self.assertAlmostEqual(values["EyeLeftX"], 0.18)
        self.assertAlmostEqual(values["Brows"], 0.605)
        self.assertAlmostEqual(values["MouthSmile"], 0.9592)
        self.assertAlmostEqual(values["EyeOpenLeft"], 0.4736835)

    def test_soullink_neutral_frame_uses_model_tracking_neutral_points(self):
        plugin = self._plugin(soullink_enabled=True, soullink_vts_mapping="{}")

        parameters = plugin._map_soullink_frame_to_vts(
            {
                "live2dParams": {
                    "eyeOpen": 1.0,
                    "mouthSmile": 0.04,
                    "mouthFrown": 0.0,
                    "browInnerUp": 0.0,
                    "browOuterUp": 0.0,
                    "browDown": 0.0,
                }
            }
        )
        values = {item["id"]: item["value"] for item in parameters}

        self.assertAlmostEqual(values["Brows"], 0.5)
        self.assertAlmostEqual(values["MouthSmile"], 0.78)
        self.assertAlmostEqual(values["EyeOpenLeft"], 0.526315)
        self.assertAlmostEqual(values["EyeOpenRight"], 0.526315)

    def test_soullink_prefers_smoothed_params_and_mixes_body_motion(self):
        plugin = self._plugin(soullink_enabled=True, soullink_vts_mapping="{}")

        parameters = plugin._map_soullink_frame_to_vts(
            {
                "facs": {"headX": -0.8, "bodyX": 0.0},
                "live2dParams": {
                    "headX": 0.04,
                    "bodyX": 0.06,
                    "eyeOpen": 1.0,
                    "mouthSmile": 0.04,
                },
            }
        )
        values = {item["id"]: item["value"] for item in parameters}

        self.assertAlmostEqual(values["FaceAngleX"], (0.04 + 0.06 * 0.65) * 36)

    def test_soullink_blink_reaches_individual_eye_inputs(self):
        plugin = self._plugin(soullink_enabled=True, soullink_vts_mapping="{}")

        parameters = plugin._map_soullink_frame_to_vts(
            {
                "live2dParams": {
                    "eyeOpen": 1.0,
                    "eyeBlinkL": 1.0,
                    "eyeBlinkR": 0.25,
                    "mouthSmile": 0.04,
                }
            }
        )
        values = {item["id"]: item["value"] for item in parameters}

        self.assertEqual(values["EyeOpenLeft"], 0.0)
        self.assertAlmostEqual(values["EyeOpenRight"], 0.75 * 0.526315)

    def test_legacy_mapping_keeps_negative_scale_and_weight(self):
        plugin = self._plugin(
            soullink_enabled=True,
            soullink_vts_mapping=json.dumps(
                {
                    "headX": {
                        "id": "FaceAngleX",
                        "scale": -2.0,
                        "offset": 1.0,
                        "min": -3.0,
                        "max": 4.0,
                        "weight": 0.4,
                    }
                }
            ),
        )

        parameters = plugin._map_soullink_frame_to_vts(
            {"facs": {"headX": 0.5}}
        )

        self.assertEqual(parameters[0]["id"], "FaceAngleX")
        self.assertAlmostEqual(parameters[0]["value"], 0.0)
        self.assertAlmostEqual(parameters[0]["weight"], 0.4)

    def test_advanced_mapping_supports_asymmetric_three_point_curve(self):
        mapping = {
            "version": 2,
            "rules": [
                {
                    "id": "mouth-asymmetric",
                    "source": "mouthShape",
                    "target": "MouthSmile",
                    "sourceMin": -1.0,
                    "sourceNeutral": 0.0,
                    "sourceMax": 0.5,
                    "outputMin": 0.2,
                    "outputNeutral": 0.78,
                    "outputMax": 1.0,
                    "curve": 1.0,
                }
            ],
        }
        plugin = self._plugin(
            soullink_enabled=True,
            soullink_vts_mapping=json.dumps(mapping),
        )

        positive = plugin._map_soullink_frame_to_vts(
            {"facs": {"mouthSmile": 0.54}}
        )
        negative = plugin._map_soullink_frame_to_vts(
            {"facs": {"mouthSmile": 0.04, "mouthFrown": 0.5}}
        )

        self.assertAlmostEqual(positive[0]["value"], 1.0)
        self.assertAlmostEqual(negative[0]["value"], 0.49)

    def test_advanced_mapping_can_add_multiple_sources_to_one_target(self):
        mapping = {
            "version": 2,
            "rules": [
                {
                    "id": "head",
                    "source": "headX",
                    "target": "FaceAngleX",
                    "sourceMin": -1,
                    "sourceNeutral": 0,
                    "sourceMax": 1,
                    "outputMin": -1,
                    "outputNeutral": 0,
                    "outputMax": 1,
                },
                {
                    "id": "body",
                    "source": "bodyX",
                    "target": "FaceAngleX",
                    "sourceMin": -1,
                    "sourceNeutral": 0,
                    "sourceMax": 1,
                    "outputMin": -2,
                    "outputNeutral": 0,
                    "outputMax": 2,
                    "blend": "add",
                },
            ],
        }
        plugin = self._plugin(
            soullink_enabled=True,
            soullink_vts_mapping=json.dumps(mapping),
        )

        parameters = plugin._map_soullink_frame_to_vts(
            {"facs": {"headX": 0.5, "bodyX": 0.25}}
        )

        self.assertAlmostEqual(parameters[0]["value"], 1.0)

    def test_advanced_mapping_exposes_vad_sources_and_skips_invalid_vts_input(self):
        mapping = {
            "version": 2,
            "rules": [
                {
                    "id": "valence",
                    "source": "vadValence",
                    "target": "CustomMood",
                    "sourceMin": -1,
                    "sourceNeutral": 0,
                    "sourceMax": 1,
                    "outputMin": -1,
                    "outputNeutral": 0,
                    "outputMax": 1,
                }
            ],
        }
        plugin = self._plugin(
            soullink_enabled=True,
            soullink_vts_mapping=json.dumps(mapping),
        )
        plugin._set_soullink_vts_input_catalog([{"name": "FaceAngleX"}])

        parameters = plugin._map_soullink_frame_to_vts(
            {
                "facs": {"headX": 0.0},
                "vad": {"current": {"valence": 0.75}, "intensity": 0.6},
            }
        )

        self.assertEqual(parameters, [])
        payload = plugin._soullink_mapping_editor_payload()
        self.assertIn("vadValence", {item["id"] for item in payload["sources"]})

    def test_advanced_mapping_strict_validation_rejects_inverted_source_range(self):
        plugin = self._plugin(soullink_enabled=True, soullink_vts_mapping="{}")

        with self.assertRaisesRegex(ValueError, "源范围"):
            plugin._normalize_soullink_mapping(
                {
                    "version": 2,
                    "rules": [
                        {
                            "source": "headX",
                            "target": "FaceAngleX",
                            "sourceMin": 1,
                            "sourceNeutral": 0,
                            "sourceMax": -1,
                        }
                    ],
                },
                strict=True,
            )

    async def test_parameter_scheduler_merges_layers_with_mouth_priority(self):
        scheduler = VTSParameterScheduler(
            SimpleNamespace(),
            lambda: asyncio.sleep(0, result=True),
        )
        scheduler.set_layer(
            "soullink",
            [
                {"id": "MouthSmile", "value": 0.7},
                {"id": "FaceAngleX", "value": 12.0},
            ],
        )
        scheduler.set_layer("mouth", [{"id": "MouthSmile", "value": 0.2}])

        values = {item["id"]: item["value"] for item in scheduler.merged_parameters()}

        self.assertEqual(values["MouthSmile"], 0.2)
        self.assertEqual(values["FaceAngleX"], 12.0)

    async def test_parameter_scheduler_throttles_offline_reconnects(self):
        attempts = []

        async def ensure_connection():
            attempts.append(True)
            return False

        scheduler = VTSParameterScheduler(
            SimpleNamespace(is_connected=False),
            ensure_connection,
        )
        scheduler.set_layer("soullink", [{"id": "FaceAngleX", "value": 12.0}])

        self.assertFalse(await scheduler.flush())
        self.assertFalse(await scheduler.flush())

        self.assertEqual(attempts, [True])
        self.assertEqual(scheduler.status()["connection_error"], "VTube Studio 未连接")

    async def test_parameter_scheduler_uses_only_active_source_fps(self):
        scheduler = VTSParameterScheduler(
            SimpleNamespace(),
            lambda: asyncio.sleep(0, result=True),
            fps=20,
        )
        scheduler.set_source_fps("soullink", 20)
        scheduler.set_layer("soullink", [{"id": "FaceAngleX", "value": 1.0}])
        scheduler.set_source_fps("mouth", 30)

        self.assertEqual(scheduler.effective_fps(), 20)

        scheduler.set_layer("mouth", [{"id": "MouthOpen", "value": 0.5}])
        self.assertEqual(scheduler.effective_fps(), 30)

        scheduler.clear_layer("mouth")
        self.assertEqual(scheduler.effective_fps(), 20)

    async def test_parameter_scheduler_drops_stale_realtime_layer(self):
        scheduler = VTSParameterScheduler(
            SimpleNamespace(),
            lambda: asyncio.sleep(0, result=True),
        )
        scheduler.set_layer(
            "soullink",
            [{"id": "FaceAngleX", "value": 1.0}],
            ttl=0.1,
        )
        scheduler._layer_updated_at["soullink"] = time.monotonic() - 0.2

        self.assertEqual(scheduler.merged_parameters(), [])
        self.assertEqual(scheduler.stale_layers_dropped, 1)

    async def test_vts_api_error_is_not_reported_as_success(self):
        class _FakeWebSocket:
            closed = False

            async def send(self, _payload):
                return None

            async def recv(self):
                return json.dumps(
                    {
                        "messageType": "APIError",
                        "data": {"errorID": 453, "message": "invalid parameter"},
                    }
                )

        client = VTSClient()
        client._ws = _FakeWebSocket()
        client._is_connected = True

        with self.assertRaisesRegex(VTSResponseError, "453"):
            await client.inject_parameters([{"id": "ParamMouthOpenY", "value": 0.5}])

    async def test_vts_reconnect_reauthenticates_before_parameter_frame(self):
        class _FakeWebSocket:
            closed = False

            def __init__(self):
                self.sent = []
                self.responses = deque(
                    [
                        {
                            "messageType": "AuthenticationResponse",
                            "data": {"authenticated": True},
                        },
                        {
                            "messageType": "InjectParameterDataResponse",
                            "data": {},
                        },
                    ]
                )

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def recv(self):
                return json.dumps(self.responses.popleft())

        client = VTSClient()
        socket = _FakeWebSocket()
        client.auth_token = "saved-token"

        async def connect():
            client._ws = socket
            client._is_connected = True
            client._authenticated = False

        client._connect = connect

        await client.inject_parameters([{"id": "FaceAngleX", "value": 1.0}])

        self.assertEqual(
            [item["messageType"] for item in socket.sent],
            ["AuthenticationRequest", "InjectParameterDataRequest"],
        )
        self.assertTrue(client.is_authenticated)

    async def test_cancelled_reconnect_authentication_discards_socket(self):
        class _FakeWebSocket:
            closed = False

            def __init__(self):
                self.waiting = asyncio.Event()

            async def send(self, _payload):
                return None

            async def recv(self):
                self.waiting.set()
                await asyncio.Event().wait()

            async def close(self):
                self.closed = True

        client = VTSClient()
        socket = _FakeWebSocket()
        client.auth_token = "saved-token"

        async def connect():
            client._ws = socket
            client._is_connected = True
            client._authenticated = False

        client._connect = connect
        task = asyncio.create_task(
            client.inject_parameters([{"id": "FaceAngleX", "value": 1.0}])
        )
        await socket.waiting.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertIsNone(client._ws)
        self.assertFalse(client.is_connected)

    async def test_vts_authentication_api_error_invalidates_session(self):
        class _FakeWebSocket:
            closed = False

            async def send(self, _payload):
                return None

            async def recv(self):
                return json.dumps(
                    {
                        "messageType": "APIError",
                        "data": {"errorID": 8, "message": "Authentication required"},
                    }
                )

        client = VTSClient()
        client._ws = _FakeWebSocket()
        client._is_connected = True
        client._authenticated = True

        with self.assertRaises(VTSResponseError):
            await client.inject_parameters([{"id": "FaceAngleX", "value": 1.0}])

        self.assertFalse(client.is_authenticated)

    async def test_realtime_parameter_connection_uses_saved_token(self):
        class _FakeParameterVTS:
            is_authenticated = False

            def __init__(self):
                self.tokens = []

            async def authenticate(self, token):
                self.tokens.append(token)
                self.is_authenticated = True
                return True

        plugin = object.__new__(VTubeStudioPlugin)
        plugin._parameter_vts = _FakeParameterVTS()

        async def load_token():
            return "saved-token"

        plugin._load_token = load_token

        self.assertTrue(await plugin._check_parameter_connection())
        self.assertEqual(plugin._parameter_vts.tokens, ["saved-token"])

    async def test_realtime_parameter_frames_do_not_wait_for_responses(self):
        class _SlowResponseWebSocket:
            closed = False

            def __init__(self):
                self.sent = []
                self.recv_started = asyncio.Event()

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def recv(self):
                self.recv_started.set()
                await asyncio.Event().wait()

            async def close(self):
                self.closed = True

        client = VTSRealtimeClient()
        socket = _SlowResponseWebSocket()
        client._ws = socket
        client._is_connected = True
        client._authenticated = True

        for value in range(5):
            await asyncio.wait_for(
                client.inject_parameters(
                    [{"id": "FaceAngleX", "value": float(value)}]
                ),
                timeout=0.1,
            )

        await socket.recv_started.wait()
        self.assertEqual(len(socket.sent), 5)
        await client.disconnect()


if __name__ == "__main__":
    unittest.main()
