"""
AstrBot 插件：我会直播圈米养你
将直播弹幕、Live2D 演出、字幕和嘴型联动组织成一套直播陪伴体验。
"""

import asyncio
import copy
from collections import deque
import importlib
import json
import math
import platform
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from astrbot.api.star import Star, Context, register
from astrbot.api import llm_tool, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api import logger
from astrbot.api.message_components import Plain, Record
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import AssistantMessageSegment
from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from .vts_client import (
    VTSClient,
    VTSClientError,
    VTSConnectionError,
    VTSTimeoutError,
)
from .vts_discovery import auto_discover, get_install_info
from .bilibili_live import (
    BilibiliBlivedmClient,
    BilibiliLaplaceClient,
    BilibiliLiveClient,
    BilibiliLiveArea,
    BilibiliOpenLiveClient,
    LiveDanmakuEvent,
    fetch_bilibili_live_areas,
    probe_bilibili_live_room,
)
from .l2d_mixin import Live2DMixin
from .mouth_sync_mixin import MouthSyncMixin
from .subtitle_mixin import SubtitleMixin

# 默认配置
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8001
KV_KEY_TOKEN = "vts_auth_token"
KV_KEY_BILI_REPLY_SESSION = "bili_live_reply_session"

class SyntheticBiliLiveWakeEvent(AstrMessageEvent):
    def __init__(
        self,
        *,
        template_event: Optional[AstrMessageEvent],
        context: Context,
        session: MessageSession,
        message: str,
    ) -> None:
        message_obj = AstrBotMessage()
        message_obj.type = session.message_type
        message_obj.self_id = session.session_id
        message_obj.session_id = session.session_id
        message_obj.message_id = f"bili_live_auto_{uuid.uuid4().hex}"
        message_obj.sender = MessageMember(user_id=session.session_id, nickname="BiliLive")
        message_obj.message = [Plain(message)]
        message_obj.message_str = message
        message_obj.raw_message = message
        message_obj.timestamp = int(time.time())

        platform_meta = None
        if template_event:
            try:
                platform_meta = template_event.get_platform_metadata()
            except Exception:
                platform_meta = getattr(template_event, "platform_meta", None)
        if platform_meta is None:
            platform_meta = PlatformMetadata(
                name=session.platform_id,
                description="SyntheticBiliLiveWake",
                id=session.platform_id,
            )
        super().__init__(message, message_obj, platform_meta, session.session_id)
        self.session = session
        self.context_obj = context
        self.is_at_or_wake_command = True
        self.is_wake = True


@register(
    "astrbot_plugin_live_stream_companion",
    "menglimi",
    "B 站直播弹幕读取、自动回应、Live2D 表情动作、OBS 字幕和 TTS 嘴型联动",
    "1.6.2",
    "https://github.com/menglimi/astrbot_plugin_live_stream_companion",
)
class VTubeStudioPlugin(SubtitleMixin, MouthSyncMixin, Live2DMixin, Star):
    """直播陪伴与 Live2D 演出控制插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        self._auto_discover: bool = self.config.get("auto_discover", True)
        self._manual_host: Optional[str] = self.config.get("vts_host") or None

        # 安全解析端口，防止非数字字符串导致 ValueError
        port_val = self.config.get("vts_port")
        self._manual_port: Optional[int] = self._safe_parse_port(port_val)

        self._auto_connect: bool = self.config.get("auto_connect", True)
        self._debug_mode: bool = self.config.get("debug_mode", False)
        self._bili_debug_mode: bool = bool(self.config.get("bili_live_debug_log", False))
        self._l2d_tasks: set[asyncio.Task] = set()
        self._mouth_sync_tasks: set[asyncio.Task] = set()
        self._bili_live_client: Optional[
            BilibiliBlivedmClient
            | BilibiliLaplaceClient
            | BilibiliLiveClient
            | BilibiliOpenLiveClient
        ] = None
        self._bili_live_task: Optional[asyncio.Task] = None
        cache_size = max(10, int(self.config.get("bili_live_cache_size", 80) or 80))
        self._bili_events: deque[LiveDanmakuEvent] = deque(maxlen=cache_size)
        self._bili_session_events: deque[LiveDanmakuEvent] = deque(maxlen=500)
        self._bili_pending_reply_events: deque[LiveDanmakuEvent] = deque(maxlen=50)
        self._bili_auto_reply_task: Optional[asyncio.Task] = None
        self._bili_last_auto_reply_at = 0.0
        self._bili_auto_reply_minute_marks: deque[float] = deque(maxlen=120)
        self._bili_session_started_at = 0.0
        self._bili_summary_written_for_session = False
        self._private_companion_writeback_seen: set[str] = set()
        self._private_companion_last_state_at = 0.0
        self._bili_reply_event_template: Optional[AstrMessageEvent] = None
        self._bili_area_by_id: dict[int, BilibiliLiveArea] = {}
        self._bili_area_by_key: dict[str, BilibiliLiveArea] = {}
        self._bili_area_loaded_at = 0.0
        self._bili_area_load_task: Optional[asyncio.Task] = None
        self._private_companion_proactive_registered = False
        self._private_companion_proactive_register_task: Optional[asyncio.Task] = None
        self._subtitle_server = None
        self._warned_bili_blivedm_fallback = False
        self.page_api = None
        self._register_page_api_if_available()

        self.vts = VTSClient(
            host=self._manual_host or DEFAULT_HOST,
            port=self._manual_port or DEFAULT_PORT,
            plugin_name="AstrBot Live Stream Companion",
            plugin_developer="menglimi",
        )
        self._connected = False

    def _register_page_api_if_available(self) -> None:
        try:
            if not callable(getattr(self.context, "register_web_api", None)):
                return
            from .page_api import LiveStreamCompanionPageApi

            self.page_api = LiveStreamCompanionPageApi(self)
            self.page_api.register_routes()
            logger.info("[B站直播] 已注册插件拓展页 API。")
        except Exception as e:
            logger.debug(f"[B站直播] 注册插件拓展页 API 失败: {e}")

    def _safe_parse_port(self, port_val) -> Optional[int]:
        """安全解析端口值，防止非数字字符串导致异常"""
        if port_val is None:
            return None
        try:
            return int(port_val)
        except (ValueError, TypeError):
            logger.warning(f"[VTS] 无效的端口配置值: {port_val}，将使用默认端口")
            return None

    # ------------------------------------------------------------------ #
    #  插件生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self):
        """插件启动时：自动发现 VTS 位置，然后尝试认证连接"""
        try:
            host, port = await self._discover()
            self.vts.url = f"ws://{host}:{port}"
            # 使用公开方法重置连接，不直接操作私有属性
            await self.vts.reset_connection()

            if self._auto_connect:
                await self._try_connect()
            else:
                logger.info("[VTS] auto_connect 关闭，跳过自动连接")

            await self._start_subtitle_server_if_enabled()
            await self._ensure_bili_area_cache()
            self._start_private_companion_proactive_registration()

            if self._is_bili_live_enabled() and self.config.get(
                "bili_live_auto_start", True
            ):
                bili_type = self._get_bili_live_type()
                room_id = self._get_config_room_id()
                if room_id or bili_type in {"laplace", "open_live"}:
                    await self._start_bili_live(room_id)
                else:
                    logger.warning("[B站直播] 已开启自动启动，但未配置房间号")
        except Exception as e:
            logger.error(f"[VTS] 初始化失败: {e}")

    async def terminate(self):
        """插件卸载/停用时：断开 VTS 连接，清理资源"""
        try:
            for task in list(self._l2d_tasks):
                task.cancel()
            self._l2d_tasks.clear()
            for task in list(self._mouth_sync_tasks):
                task.cancel()
            self._mouth_sync_tasks.clear()
            if self._private_companion_proactive_register_task:
                task = self._private_companion_proactive_register_task
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                self._private_companion_proactive_register_task = None
            self._unregister_private_companion_proactive_abilities()
            await self._stop_bili_live()
            await self._stop_subtitle_server()
            await self.vts.disconnect()
            logger.info("[VTS] 插件已卸载，VTS 连接已关闭")
        except Exception as e:
            logger.warning(f"[VTS] 卸载时断开连接失败: {e}")

    async def _discover(self) -> tuple:
        """确定要连接的 host:port"""
        if self._manual_host and self._manual_port:
            logger.info(f"[VTS] 使用手动配置：{self._manual_host}:{self._manual_port}")
            return self._manual_host, self._manual_port

        if self._auto_discover:
            logger.info(f"[VTS] 开启自动发现（平台: {platform.system()}）")

        host, port = await auto_discover(host=self._manual_host or DEFAULT_HOST)
        logger.info(f"[VTS] 自动发现结果：{host}:{port}")
        return host, port

    async def _try_connect(self):
        """尝试连接并使用已保存的 Token 认证"""
        try:
            saved_token = await self._load_token()
            if saved_token:
                ok = await self.vts.authenticate(saved_token)
                if ok:
                    self._connected = True
                    logger.info("[VTS] 使用已保存 Token 认证成功")
                    return
            logger.info("[VTS] 未找到有效 Token，请发送 /vts_auth 进行认证")
        except VTSConnectionError as e:
            logger.warning(f"[VTS] 连接失败: {e}")
        except VTSTimeoutError as e:
            logger.warning(f"[VTS] 连接超时: {e}")
        except Exception as e:
            logger.warning(f"[VTS] 自动连接失败（VTube Studio 可能未启动）: {e}")

    async def _check_and_reconnect(self) -> bool:
        """检查连接状态，必要时尝试重连"""
        if self.vts.is_connected:
            return True
        try:
            saved_token = await self._load_token()
            if saved_token:
                ok = await self.vts.authenticate(saved_token)
                if ok:
                    self._connected = True
                    return True
        except Exception:
            pass
        self._connected = False
        return False

    # ------------------------------------------------------------------ #
    #  字幕与嘴型命令入口
    # ------------------------------------------------------------------ #

    @filter.command("subtitle_status")
    async def cmd_subtitle_status(self, event: AstrMessageEvent):
        """查看字幕 overlay 状态。"""
        enabled = self._is_subtitle_enabled()
        running = self._subtitle_server is not None
        url = self._subtitle_server.url if self._subtitle_server else (
            f"http://{self.config.get('subtitle_host') or '127.0.0.1'}:"
            f"{self._safe_parse_int(self.config.get('subtitle_port'), 18081)}/"
        )
        yield event.plain_result(
            f"字幕功能：{'已启用' if enabled else '未启用'}\n"
            f"字幕服务：{'运行中' if running else '未运行'}\n"
            f"Overlay 地址：{url}"
        )

    @filter.command("subtitle_test")
    async def cmd_subtitle_test(self, event: AstrMessageEvent, text: str = ""):
        """测试打字机字幕。"""
        if not self._is_subtitle_enabled():
            yield event.plain_result("字幕功能未启用，请先在插件配置中开启 subtitle_enabled。")
            return
        await self._push_subtitle(text or "这是一条打字机字幕测试。", source="manual")
        yield event.plain_result("已发送字幕测试。")

    @filter.command("subtitle_clear")
    async def cmd_subtitle_clear(self, event: AstrMessageEvent):
        """清空字幕 overlay。"""
        if self._subtitle_server:
            await self._subtitle_server.clear()
        yield event.plain_result("已清空字幕。")

    @filter.command("mouth_sync_test")
    async def cmd_mouth_sync_test(self, event: AstrMessageEvent, duration: float = 2.0):
        """测试 VTS 嘴部开闭参数联动。"""
        if not self._is_mouth_sync_enabled():
            yield event.plain_result("嘴型联动未启用，请先在插件配置中开启 mouth_sync_enabled。")
            return
        if not await self._check_and_reconnect():
            yield event.plain_result("VTube Studio 未连接，无法测试嘴型联动。")
            return

        duration = max(0.5, min(10.0, self._safe_parse_float(duration, 2.0)))
        fps = max(5, min(60, self._safe_parse_int(self.config.get("mouth_sync_fps"), 30)))
        steps = max(1, int(duration * fps))
        envelope = [
            max(0.0, math.sin(index * 0.48))
            * (0.35 + 0.45 * math.sin(index * 0.13) ** 2)
            for index in range(steps)
        ]
        self._create_mouth_sync_task(
            self._run_mouth_sync_envelope(envelope, 1.0 / fps)
        )
        yield event.plain_result(f"已启动 {duration:g} 秒嘴型联动测试。")

    # ------------------------------------------------------------------ #
    #  B站直播弹幕读取
    # ------------------------------------------------------------------ #

    def _safe_parse_int(self, value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _safe_parse_float(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _is_bili_live_enabled(self) -> bool:
        return bool(self.config.get("bilibili_enabled", False))

    def _get_config_room_id(self) -> int:
        return self._safe_parse_int(
            self.config.get("bilibili_room_id")
            or self.config.get("bili_live_room_id"),
            0,
        )

    def _get_bili_live_type(self) -> str:
        return str(
            self.config.get("bilibili_type")
            or self.config.get("bili_live_type")
            or "web"
        ).strip()

    def _get_bili_sessdata(self) -> str:
        return str(
            self.config.get("bilibili_sessdata")
            or self.config.get("bili_live_sessdata")
            or ""
        ).strip()

    def _get_bili_web_backend(self) -> str:
        configured = str(
            self.config.get("bilibili_web_backend") or "builtin"
        ).strip().lower()
        if configured == "blivedm":
            if not self._warned_bili_blivedm_fallback:
                self._warned_bili_blivedm_fallback = True
                logger.warning(
                    "[B站直播] blivedm 后端在当前环境中可能无法收到事件，已自动切换到 builtin 后端。"
                )
            return "builtin"
        return configured

    def _get_bili_open_live_config(self) -> dict[str, Any]:
        return {
            "access_key_id": str(
                self.config.get("bilibili_ACCESS_KEY_ID") or ""
            ).strip(),
            "access_key_secret": str(
                self.config.get("bilibili_ACCESS_KEY_SECRET") or ""
            ).strip(),
            "app_id": self._safe_parse_int(self.config.get("bilibili_APP_ID"), 0),
            "room_owner_auth_code": str(
                self.config.get("bilibili_ROOM_OWNER_AUTH_CODE") or ""
            ).strip(),
        }

    def _get_laplace_config(self) -> dict[str, Any]:
        bridge_url = str(
            self.config.get("laplace_event_bridge_url")
            or self.config.get("bili_live_laplace_url")
            or ""
        ).strip()
        if not bridge_url:
            host = str(self.config.get("laplace_event_bridge_host") or "localhost").strip()
            port = self._safe_parse_int(
                self.config.get("laplace_event_bridge_port"), 9696
            )
            bridge_url = f"ws://{host}:{port}"
        return {
            "bridge_url": bridge_url,
            "token": str(
                self.config.get("laplace_event_bridge_token")
                or self.config.get("bili_live_laplace_token")
                or ""
            ).strip(),
        }

    async def _ensure_bili_area_cache(self, force: bool = False) -> bool:
        if self._bili_area_by_id and not force:
            return True
        if self._bili_area_load_task and not self._bili_area_load_task.done():
            try:
                await self._bili_area_load_task
            except Exception:
                return bool(self._bili_area_by_id)
            return bool(self._bili_area_by_id)

        self._bili_area_load_task = asyncio.create_task(self._load_bili_area_cache())
        try:
            await self._bili_area_load_task
        except Exception:
            return bool(self._bili_area_by_id)
        return bool(self._bili_area_by_id)

    async def _load_bili_area_cache(self) -> None:
        try:
            areas = await fetch_bilibili_live_areas()
        except Exception as e:
            logger.warning(f"[B站直播] 直播分区列表加载失败: {e}")
            return

        by_id: dict[int, BilibiliLiveArea] = {}
        by_key: dict[str, BilibiliLiveArea] = {}
        for area in areas:
            by_id[area.area_id] = area
            for key in self._bili_area_lookup_keys(area):
                by_key.setdefault(key, area)

        self._bili_area_by_id = by_id
        self._bili_area_by_key = by_key
        self._bili_area_loaded_at = time.time()
        logger.info(f"[B站直播] 已加载直播分区列表: {len(by_id)} 个子分区")

    def _bili_area_lookup_keys(self, area: BilibiliLiveArea) -> list[str]:
        keys = [
            str(area.area_id),
            area.area_name,
            area.pinyin,
            f"{area.part_name}/{area.area_name}",
        ]
        return [self._normalize_bili_area_query(key) for key in keys if key]

    def _normalize_bili_area_query(self, query: Any) -> str:
        return re.sub(r"\s+", "", str(query or "").strip().lower())

    async def _find_bili_area(self, query: Any) -> Optional[BilibiliLiveArea]:
        text = str(query or "").strip()
        if not text:
            return None
        await self._ensure_bili_area_cache()
        area_id = self._safe_parse_int(text, 0)
        if area_id and area_id in self._bili_area_by_id:
            return self._bili_area_by_id[area_id]
        return self._bili_area_by_key.get(self._normalize_bili_area_query(text))

    async def _persist_plugin_config_updates(self, updates: dict[str, Any]) -> bool:
        if not updates:
            return True
        manager = getattr(getattr(self, "page_api", None), "config_manager", None)
        if manager is None:
            try:
                from .page_config import PageConfigManager

                manager = PageConfigManager(
                    self,
                    "astrbot_plugin_live_stream_companion",
                    logger,
                )
            except Exception:
                manager = None

        if manager is not None and callable(getattr(manager, "apply_updates", None)):
            return bool(await manager.apply_updates(updates))

        for key, value in updates.items():
            self.config[key] = value
        return False

    def _start_private_companion_proactive_registration(self) -> None:
        if (
            self._private_companion_proactive_register_task
            and not self._private_companion_proactive_register_task.done()
        ):
            return
        self._private_companion_proactive_register_task = asyncio.create_task(
            self._register_private_companion_proactive_abilities_with_retry()
        )

    async def _register_private_companion_proactive_abilities_with_retry(self) -> None:
        for attempt in range(12):
            if self._register_private_companion_proactive_abilities():
                return
            await asyncio.sleep(5 if attempt else 1)

    def _private_companion_extension_api(self) -> Any | None:
        try:
            module = importlib.import_module(
                "data.plugins.astrbot_plugin_private_companion.main"
            )
            get_api = getattr(module, "get_private_companion_api", None)
            return get_api() if callable(get_api) else None
        except Exception as e:
            logger.debug(f"[B站直播] 读取陪伴插件外部能力 API 失败: {e}")
        return None

    def _register_private_companion_proactive_abilities(self) -> bool:
        api = self._private_companion_extension_api()
        register_ability = getattr(api, "register_proactive_ability", None)
        if not callable(register_ability):
            return False
        ok_start = register_ability(
            {
                "name": "live_stream_start",
                "module": "直播陪伴",
                "label": "准备开播",
                "description": "在合适时机准备直播，选择分区、拟定标题，并可按配置启动监听或 OBS 推流。",
                "when": "当前日程、心情或话题适合和直播间观众互动，且直播环境已经准备好时",
                "use_for": "形成一场直播的开场素材、现场感和可分享的生活事件",
                "avoid": "不要暴露 OBS、插件、接口、配置字段或执行过程；未真正推流时不要说已经开播",
                "share_probability": 0.04,
                "min_interval_hours": 24,
                "default_enabled": False,
                "default_config": {
                    "area_query": "",
                    "title_template": "",
                    "start_listener": True,
                    "start_apps": True,
                    "start_obs_stream": False,
                    "update_area_config": True,
                    "scene": "",
                    "wait_seconds": 5,
                },
                "config_schema": {
                    "area_query": {
                        "label": "默认分区",
                        "description": "可填子分区名、拼音或 area_id；留空使用直播插件当前 area_id",
                    },
                    "title_template": {
                        "label": "标题模板",
                        "description": "支持 {area_name}、{part_name}、{bot_name}、{display_name}、{reason}、{plan}",
                    },
                    "start_listener": {
                        "label": "启动弹幕监听",
                        "description": "执行时调用直播插件的 B 站弹幕监听",
                    },
                    "start_apps": {
                        "label": "启动 OBS/L2DStudio",
                        "description": "执行前尝试打开已配置的 OBS 和 L2DStudio",
                    },
                    "start_obs_stream": {
                        "label": "启动 OBS 推流",
                        "description": "危险动作；还需要直播插件 obs_allow_stream_start 为 true",
                    },
                    "update_area_config": {
                        "label": "写回分区配置",
                        "description": "用默认分区反查到 part_id/area_id 后写回直播插件配置",
                    },
                    "scene": {
                        "label": "OBS 场景",
                        "description": "留空使用直播插件默认直播场景",
                    },
                    "wait_seconds": {
                        "label": "启动等待秒数",
                        "description": "打开程序后等待 OBS WebSocket 就绪的时间",
                    },
                },
                "executor": self._execute_private_companion_start_live_ability,
            }
        )
        ok_stop = register_ability(
            {
                "name": "live_stream_stop",
                "module": "直播陪伴",
                "label": "结束直播",
                "description": "在合适时机收束直播，可按配置停止 OBS 推流和弹幕监听并触发下播小结。",
                "when": "当前直播已经接近尾声、日程切换、能量下降或需要收束现场互动时",
                "use_for": "整理直播余韵、结束监听、沉淀下播小结",
                "avoid": "不要暴露 OBS、插件、接口、配置字段或执行过程；未真正推流时不要说已经下播",
                "share_probability": 0.03,
                "min_interval_hours": 12,
                "default_enabled": False,
                "default_config": {
                    "stop_listener": True,
                    "stop_obs_stream": False,
                },
                "config_schema": {
                    "stop_listener": {
                        "label": "停止弹幕监听",
                        "description": "执行时调用直播插件停止 B 站弹幕监听",
                    },
                    "stop_obs_stream": {
                        "label": "停止 OBS 推流",
                        "description": "危险动作；开启后会调用 OBS StopStream",
                    },
                },
                "executor": self._execute_private_companion_stop_live_ability,
            }
        )
        self._private_companion_proactive_registered = bool(ok_start and ok_stop)
        if self._private_companion_proactive_registered:
            logger.info("[B站直播] 已向陪伴插件注册主动开播/下播外部能力。")
        return self._private_companion_proactive_registered

    def _unregister_private_companion_proactive_abilities(self) -> None:
        api = self._private_companion_extension_api()
        unregister = getattr(api, "unregister_proactive_ability", None)
        if not callable(unregister):
            return
        for name in ("live_stream_start", "live_stream_stop"):
            try:
                unregister(name)
            except Exception as e:
                logger.debug(f"[B站直播] 注销陪伴插件外部能力失败 {name}: {e}")
        self._private_companion_proactive_registered = False

    async def _execute_private_companion_start_live_ability(
        self, ctx: dict[str, Any]
    ) -> dict[str, Any]:
        ability_config = ctx.get("config") if isinstance(ctx.get("config"), dict) else {}
        messages: list[str] = []
        area = await self._resolve_proactive_live_area(ability_config)
        title = self._draft_proactive_live_title(ctx, ability_config, area)

        if self._config_bool(ability_config.get("start_listener"), True):
            room_id = self._get_config_room_id()
            if self._get_bili_live_type() == "web" and not room_id:
                messages.append("未配置 B站直播房间号，已跳过弹幕监听")
            else:
                messages.append(await self._start_bili_live(room_id))

        if self._config_bool(ability_config.get("start_obs_stream"), False):
            messages.extend(await self._start_obs_stream_for_proactive(ability_config))
        elif self._config_bool(ability_config.get("start_apps"), True):
            messages.extend(await self._start_live_apps_for_proactive(ability_config))

        area_text = area.display_text() if area else "未指定分区"
        context = (
            f"直播准备：分区 {area_text}；拟定标题《{title}》。"
            f"{'；'.join(item for item in messages if item)}"
        )
        return {
            "ok": True,
            "context": context,
            "summary": f"准备直播：{title}",
            "memory": f"准备了一场直播，分区是 {area_text}，标题草案是《{title}》。",
            "status": context,
        }

    async def _execute_private_companion_stop_live_ability(
        self, ctx: dict[str, Any]
    ) -> dict[str, Any]:
        ability_config = ctx.get("config") if isinstance(ctx.get("config"), dict) else {}
        messages: list[str] = []
        if self._config_bool(ability_config.get("stop_obs_stream"), False):
            messages.extend(await self._stop_obs_stream_for_proactive())
        if self._config_bool(ability_config.get("stop_listener"), True):
            messages.append(await self._stop_bili_live())
        if not messages:
            messages.append("没有启用具体下播动作，只记录了下播意图")
        context = "直播收束：" + "；".join(item for item in messages if item)
        return {
            "ok": True,
            "context": context,
            "summary": "结束直播",
            "memory": "主动收束了一次直播，并把下播余韵整理进直播记忆。",
            "status": context,
        }

    async def _resolve_proactive_live_area(
        self, ability_config: dict[str, Any]
    ) -> Optional[BilibiliLiveArea]:
        query = str(ability_config.get("area_query") or "").strip()
        if not query:
            query = str(self.config.get("area_id") or "").strip()
        area = await self._find_bili_area(query) if query else None
        if area and self._config_bool(ability_config.get("update_area_config"), True):
            await self._persist_plugin_config_updates(
                {"part_id": area.part_id, "area_id": area.area_id}
            )
        return area

    def _draft_proactive_live_title(
        self,
        ctx: dict[str, Any],
        ability_config: dict[str, Any],
        area: Optional[BilibiliLiveArea],
    ) -> str:
        plan = ctx.get("current_plan_item") if isinstance(ctx.get("current_plan_item"), dict) else {}
        plan_text = self._single_line_text(
            plan.get("title") or plan.get("summary") or plan.get("activity") or "",
            32,
        )
        reason = self._single_line_text(ctx.get("reason"), 48)
        values = {
            "area_name": area.area_name if area else "闲聊",
            "part_name": area.part_name if area else "直播",
            "bot_name": self._single_line_text(ctx.get("bot_name"), 24) or "我",
            "display_name": self._single_line_text(ctx.get("display_name"), 24) or "大家",
            "reason": reason,
            "plan": plan_text,
        }
        template = str(ability_config.get("title_template") or "").strip()
        if template:
            try:
                title = template.format(**values)
            except Exception:
                title = template
        else:
            topic = plan_text or reason or values["area_name"]
            title = f"{values['area_name']}陪伴场：{topic}"
        return self._single_line_text(title, 30).strip(" ：:") or "今天也开一会儿"

    async def _start_live_apps_for_proactive(
        self, ability_config: dict[str, Any]
    ) -> list[str]:
        helper = self._page_api_helper()
        if helper is None:
            return ["拓展页控制 API 不可用，无法启动 OBS/L2DStudio"]
        messages: list[str] = []
        for app in ("obs", "l2dstudio"):
            try:
                messages.append(helper._start_configured_app(app))
            except Exception as e:
                messages.append(self._single_line_text(e, 90))
        wait_seconds = max(
            0,
            min(20, self._safe_parse_int(ability_config.get("wait_seconds"), 5)),
        )
        if wait_seconds:
            await asyncio.sleep(wait_seconds)
        return messages

    async def _start_obs_stream_for_proactive(
        self, ability_config: dict[str, Any]
    ) -> list[str]:
        if not bool(self.config.get("obs_control_enabled", False)):
            return ["OBS 开播控制未启用，已跳过推流"]
        if not bool(self.config.get("obs_allow_stream_start", False)):
            return ["直播插件未允许 OBS StartStream，已跳过推流"]
        helper = self._page_api_helper()
        if helper is None:
            return ["拓展页控制 API 不可用，无法启动 OBS 推流"]
        messages = []
        if self._config_bool(ability_config.get("start_apps"), True):
            messages.extend(await self._start_live_apps_for_proactive(ability_config))
        scene = self._single_line_text(
            ability_config.get("scene") or self.config.get("obs_live_scene_name"),
            120,
        )
        if scene:
            await helper._obs_request("SetCurrentProgramScene", {"sceneName": scene})
            messages.append(f"OBS 已切换到场景：{scene}")
        status = await helper._obs_control_status(check_obs_ws=True)
        if ((status.get("obs") or {}).get("streaming")):
            messages.append("OBS 已在推流中")
            return messages
        await helper._obs_request("StartStream")
        messages.append("OBS 推流已开始")
        return messages

    async def _stop_obs_stream_for_proactive(self) -> list[str]:
        if not bool(self.config.get("obs_control_enabled", False)):
            return ["OBS 开播控制未启用，已跳过停止推流"]
        helper = self._page_api_helper()
        if helper is None:
            return ["拓展页控制 API 不可用，无法停止 OBS 推流"]
        status = await helper._obs_control_status(check_obs_ws=True)
        if not ((status.get("obs") or {}).get("streaming")):
            return ["OBS 当前未推流"]
        await helper._obs_request("StopStream")
        return ["OBS 推流已停止"]

    def _page_api_helper(self) -> Any | None:
        if self.page_api is not None:
            return self.page_api
        try:
            from .page_api import LiveStreamCompanionPageApi

            self.page_api = LiveStreamCompanionPageApi(self)
            return self.page_api
        except Exception as e:
            logger.debug(f"[B站直播] 创建拓展页控制 helper 失败: {e}")
            return None

    def _config_bool(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "on", "开启"}:
                return True
            if text in {"0", "false", "no", "off", "关闭"}:
                return False
        return bool(value)

    async def _start_bili_live(self, room_id: int) -> str:
        if not self._is_bili_live_enabled():
            return "B站直播功能未启用，请先在插件配置中开启 bilibili_enabled。"

        if self._bili_live_task and not self._bili_live_task.done():
            return "B站直播弹幕监听已在运行。"

        bili_type = self._get_bili_live_type()
        if bili_type == "laplace":
            laplace_cfg = self._get_laplace_config()
            self._bili_live_client = BilibiliLaplaceClient(
                bridge_url=laplace_cfg["bridge_url"],
                room_id=room_id,
                token=laplace_cfg["token"],
                on_event=self._on_bili_live_event,
                debug_log=self._bili_debug_mode,
            )
        elif bili_type == "web":
            sessdata = self._get_bili_sessdata()
            web_backend = self._get_bili_web_backend()
            if web_backend == "laplace":
                laplace_cfg = self._get_laplace_config()
                self._bili_live_client = BilibiliLaplaceClient(
                    bridge_url=laplace_cfg["bridge_url"],
                    room_id=room_id,
                    token=laplace_cfg["token"],
                    on_event=self._on_bili_live_event,
                    debug_log=self._bili_debug_mode,
                )
            elif web_backend in {"builtin", "history"}:
                self._bili_live_client = BilibiliLiveClient(
                    room_id=room_id,
                    sessdata=sessdata,
                    on_event=self._on_bili_live_event,
                    debug_log=self._bili_debug_mode,
                    history_poll_interval=self._safe_parse_float(
                        self.config.get("bili_live_history_poll_interval"), 3.0
                    ),
                    websocket_enabled=web_backend != "history",
                )
            else:
                self._bili_live_client = BilibiliBlivedmClient(
                    room_id=room_id,
                    sessdata=sessdata,
                    on_event=self._on_bili_live_event,
                    debug_log=self._bili_debug_mode,
                )
        elif bili_type == "open_live":
            open_cfg = self._get_bili_open_live_config()
            missing = [
                key
                for key, value in open_cfg.items()
                if not value
            ]
            if missing:
                return (
                    "B站开放平台配置不完整，请填写："
                    + ", ".join(missing)
                )
            self._bili_live_client = BilibiliOpenLiveClient(
                access_key_id=open_cfg["access_key_id"],
                access_key_secret=open_cfg["access_key_secret"],
                app_id=open_cfg["app_id"],
                room_owner_auth_code=open_cfg["room_owner_auth_code"],
                on_event=self._on_bili_live_event,
            )
        else:
            return f"不支持的 B站直播监听类型: {bili_type}"

        self._bili_session_started_at = time.time()
        self._bili_session_events.clear()
        self._bili_summary_written_for_session = False
        self._private_companion_writeback_seen.clear()
        self._bili_live_task = asyncio.create_task(self._bili_live_client.run_forever())
        self._bili_live_task.add_done_callback(self._on_bili_live_task_done)
        backend_text = (
            f"/{self._get_bili_web_backend()}" if bili_type == "web" else ""
        )
        logger.info(f"[B站直播] 已启动 {bili_type}{backend_text} 弹幕监听")
        room_text = f"，房间号：{room_id}" if bili_type == "web" else ""
        return f"已启动 B站直播弹幕监听（{bili_type}{backend_text}）{room_text}"

    async def _stop_bili_live(self) -> str:
        await self._write_private_companion_live_summary()
        if self._bili_live_client:
            await self._bili_live_client.stop()
            self._bili_live_client = None

        if self._bili_live_task:
            if not self._bili_live_task.done():
                self._bili_live_task.cancel()
                try:
                    await self._bili_live_task
                except asyncio.CancelledError:
                    pass
            else:
                try:
                    self._bili_live_task.exception()
                except BaseException:
                    pass
            self._bili_live_task = None

        return "已停止 B站直播弹幕监听。"

    def _on_bili_live_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning(f"[B站直播] 弹幕监听任务结束: {exc}")
        if self._bili_session_events:
            asyncio.create_task(self._write_private_companion_live_summary())

    async def _on_bili_live_event(self, event: LiveDanmakuEvent) -> None:
        self._bili_events.append(event)
        self._bili_session_events.append(event)
        await self._write_private_companion_live_event(event)
        if self._should_collect_for_auto_reply(event):
            self._bili_pending_reply_events.append(event)
            self._schedule_bili_auto_reply()
        if self.config.get("bili_live_log_events", True):
            logger.info(
                f"[B站直播] 捕获事件 room={self._get_current_bili_room_text()} "
                f"type={event.event_type} {event.display_text()}"
            )
        elif self._debug_mode or self._bili_debug_mode:
            logger.debug(f"[B站直播] {event.event_type}: {event.display_text()}")

    def _get_current_bili_room_text(self) -> str:
        if not self._bili_live_client:
            return str(self._get_config_room_id() or "未知")
        room_id = getattr(self._bili_live_client, "real_room_id", None)
        if room_id:
            return str(room_id)
        return str(self._get_config_room_id() or "未知")

    def _should_collect_for_auto_reply(self, event: LiveDanmakuEvent) -> bool:
        if not self.config.get("bili_live_auto_reply_enabled", False):
            return False
        event_types = self.config.get("bili_live_auto_reply_event_types", ["danmaku"])
        if not isinstance(event_types, list):
            event_types = ["danmaku"]
        return event.event_type in {str(item).strip() for item in event_types}

    def _schedule_bili_auto_reply(self) -> None:
        if self._bili_auto_reply_task and not self._bili_auto_reply_task.done():
            return
        self._bili_auto_reply_task = asyncio.create_task(self._bili_auto_reply_worker())

    async def _bili_auto_reply_worker(self) -> None:
        try:
            cooldown = max(
                1.0,
                self._safe_parse_float(
                    self.config.get("bili_live_auto_reply_cooldown_seconds"), 12.0
                ),
            )
            elapsed = time.time() - self._bili_last_auto_reply_at
            if elapsed < cooldown:
                await asyncio.sleep(cooldown - elapsed)

            min_events = max(
                1,
                self._safe_parse_int(
                    self.config.get("bili_live_auto_reply_min_events"), 1
                ),
            )
            if len(self._bili_pending_reply_events) < min_events:
                return

            events = list(self._bili_pending_reply_events)
            self._bili_pending_reply_events.clear()
            await self._reply_to_bili_live_events(events)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[B站直播] 自动回应弹幕失败: {e}")

    async def _get_bili_reply_session(self) -> str:
        configured = str(self.config.get("bili_live_auto_reply_session_id") or "").strip()
        if configured:
            return configured
        fallback = self._default_bili_reply_session_to_self()
        if fallback:
            logger.info("[B站直播] 未显式配置自动回应会话，默认使用 Bot 自己的私聊会话: %s", fallback)
            return fallback
        bound = str(await self.get_kv_data(KV_KEY_BILI_REPLY_SESSION, "") or "").strip()
        if bound:
            return bound
        return ""

    def _default_bili_reply_session_to_self(self) -> str:
        platform_manager = getattr(self.context, "platform_manager", None)
        platform_insts = list(getattr(platform_manager, "platform_insts", []) or [])
        candidates: list[tuple[int, str, str]] = []
        platform_ids: list[str] = []
        for inst in platform_insts:
            try:
                meta = inst.meta()
                platform_id = str(getattr(meta, "id", "") or "").strip()
            except Exception:
                platform_id = ""
            if not platform_id:
                platform_id = str(getattr(inst, "id", "") or getattr(inst, "name", "") or "").strip()
            if platform_id and platform_id not in platform_ids:
                platform_ids.append(platform_id)
            self_id = str(
                getattr(inst, "client_self_id", "")
                or getattr(inst, "self_id", "")
                or getattr(inst, "bot_self_id", "")
                or ""
            ).strip()
            if not platform_id or not self_id or not self_id.isdigit():
                continue
            priority = 0 if platform_id == "aiocqhttp" else 1
            candidates.append((priority, platform_id, self_id))
        if not candidates:
            configured_self_id = self._configured_bot_self_id()
            if not configured_self_id:
                return ""
            platform_id = platform_ids[0] if platform_ids else "default"
            return f"{platform_id}:FriendMessage:{configured_self_id}"
        _, platform_id, self_id = sorted(candidates, key=lambda item: item[0])[0]
        return f"{platform_id}:FriendMessage:{self_id}"

    def _configured_bot_self_id(self) -> str:
        config_dir = Path(__file__).resolve().parents[2] / "config"
        config_files = [
            config_dir / "astrbot_plugin_live_stream_companion_config.json",
            config_dir / "astrbot_plugin_llm_executor_config.json",
            config_dir / "astrbot_plugin_vtube_studio_config.json",
            config_dir / "astrbot_plugin_qq_group_daily_analysis_config.json",
        ]
        for path in config_files:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            for key in ("bot_user_id", "bot_self_id", "self_id"):
                value = str(data.get(key) or "").strip()
                if value.isdigit():
                    return value
            raw_ids = data.get("bot_self_ids")
            if isinstance(raw_ids, list):
                for item in raw_ids:
                    value = str(item or "").strip()
                    if value.isdigit():
                        return value
        return ""

    def _bili_auto_reply_priority_types(self) -> set[str]:
        raw = self.config.get(
            "bili_live_auto_reply_rate_limit_exempt_event_types",
            ["gift", "super_chat", "buy_guard"],
        )
        if not isinstance(raw, list):
            raw = ["gift", "super_chat", "buy_guard"]
        return {str(item).strip() for item in raw if str(item).strip()}

    def _is_bili_auto_reply_rate_exempt(self, events: list[LiveDanmakuEvent]) -> bool:
        priority_types = self._bili_auto_reply_priority_types()
        return any(event.event_type in priority_types for event in events)

    def _bili_auto_reply_rate_limited(self, events: list[LiveDanmakuEvent]) -> bool:
        if self._is_bili_auto_reply_rate_exempt(events):
            return False
        max_per_minute = self._safe_parse_int(
            self.config.get("bili_live_auto_reply_max_per_minute"),
            6,
        )
        if max_per_minute <= 0:
            return False
        now = time.time()
        while self._bili_auto_reply_minute_marks and now - self._bili_auto_reply_minute_marks[0] >= 60:
            self._bili_auto_reply_minute_marks.popleft()
        return len(self._bili_auto_reply_minute_marks) >= max_per_minute

    def _record_bili_auto_reply_rate_mark(self, events: list[LiveDanmakuEvent]) -> None:
        if self._is_bili_auto_reply_rate_exempt(events):
            return
        max_per_minute = self._safe_parse_int(
            self.config.get("bili_live_auto_reply_max_per_minute"),
            6,
        )
        if max_per_minute <= 0:
            return
        now = time.time()
        while self._bili_auto_reply_minute_marks and now - self._bili_auto_reply_minute_marks[0] >= 60:
            self._bili_auto_reply_minute_marks.popleft()
        self._bili_auto_reply_minute_marks.append(now)

    async def _reply_to_bili_live_events(self, events: list[LiveDanmakuEvent]) -> None:
        session_id = await self._get_bili_reply_session()
        if not session_id:
            logger.warning(
                "[B站直播] 已收到弹幕，但未绑定自动回应会话，也未能自动获取 Bot 自己的 QQ。请在目标聊天发送 /bili_live_bind_here。"
            )
            return

        if self._bili_auto_reply_rate_limited(events):
            logger.info(
                "[B站直播] 普通弹幕自动回应已达到每分钟上限，跳过本批 %s 条事件。",
                len(events),
            )
            return

        reply_mode = str(
            self.config.get("bili_live_auto_reply_mode") or "native"
        ).strip()
        if reply_mode == "native":
            if await self._reply_to_bili_live_events_via_framework(events, session_id):
                return
            logger.warning("[B站直播] 框架式原生自动回应失败，回退到事件队列投递。")
            await self._dispatch_bili_live_native_event(events, session_id)
            return

        provider = None
        try:
            provider = self.context.get_using_provider(session_id)
        except Exception:
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
        if not provider:
            logger.warning("[B站直播] 自动回应弹幕失败：未找到可用 LLM Provider")
            return

        max_events = max(
            1,
            self._safe_parse_int(self.config.get("bili_live_auto_reply_max_events"), 5),
        )
        selected = events[-max_events:]
        formatted = self._format_bili_events(selected)
        if not formatted:
            return

        system_prompt = str(
            self.config.get("bili_live_auto_reply_system_prompt")
            or "你是正在直播中的虚拟主播助手。请根据观众最近的弹幕自然回应，语气像实时聊天，不要逐条复读。"
        )
        prompt = (
            "请根据以下 B站直播间最新互动生成一句自然回复。\n"
            "要求：中文；像主播现场回应；不要说自己看不到弹幕；不要列清单；"
            "优先回应具体问题或反馈；控制在 15 到 60 个字；"
            "只输出要发给直播间观众的话，不要描述发送状态、处理过程或自己的回应策略。\n\n"
            f"{formatted}"
        )
        prompt += self._build_bili_support_reply_hint(selected)
        auxiliary_context = self._build_bili_live_auxiliary_context(selected)
        if auxiliary_context:
            prompt += "\n\n" + auxiliary_context
        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=f"{session_id}:bili_live_auto_reply",
            persist=False,
        )
        reply_text = self._extract_provider_text(response)
        reply_text = self._clean_auto_reply_text(reply_text)
        if not reply_text:
            return

        force_voice = bool(self.config.get("bili_live_auto_reply_force_full_tts", True))
        chain = await self._decorate_bili_live_reply_chain(
            session_id,
            [Plain(reply_text)],
            force_voice=False,
            skip_subtitle=force_voice,
        )
        chain = self._strip_tts_blocks_from_plain_chain(chain)
        chain = self._ensure_visible_text_after_voice(chain, reply_text)
        await self.context.send_message(session_id, MessageChain(chain))
        self._bili_last_auto_reply_at = time.time()
        self._record_bili_auto_reply_rate_mark(selected)
        await self._push_subtitle(reply_text, source="bili_live")
        if force_voice:
            asyncio.create_task(
                self._send_bili_live_tts_followup(
                    session_id,
                    reply_text,
                    push_subtitle=False,
                )
            )
        logger.info(f"[B站直播] 已自动回应弹幕 -> {session_id}: {reply_text}")

    async def _dispatch_bili_live_native_event(
        self, events: list[LiveDanmakuEvent], session_id: str
    ) -> None:
        if not self._bili_reply_event_template:
            logger.warning(
                "[B站直播] 自动回应设置为原生路径，但当前进程没有绑定事件模板。"
                "请在目标聊天重新发送 /bili_live_bind_here。"
            )
            return

        max_events = max(
            1,
            self._safe_parse_int(self.config.get("bili_live_auto_reply_max_events"), 5),
        )
        formatted = self._format_bili_events(events[-max_events:])
        if not formatted:
            return

        prompt = (
            "【B站直播间弹幕事件】\n"
            "请像正在直播中收到弹幕一样回应直播间观众。\n"
            "身份边界：下面的用户名是 B站直播间观众昵称，不是当前私聊对象，也不等于私聊历史里的用户或群友；"
            "不要把私聊记忆、旧对话人物、现实称呼代入当前弹幕。\n"
            "要求：自然回应，不要逐条复读；优先回应具体问题或反馈；不要说自己看不到弹幕；"
            "如果弹幕只是“摸摸/贴贴/抱抱”这类互动，就直接以主播身份回应这个观众，"
            "不要说第三个人在摸你，也不要提无关照片、日程或旧聊天。\n"
            "只输出要发给直播间观众的话，不要描述发送状态、处理过程或自己的回应策略。\n\n"
            f"{formatted}"
        )
        prompt += self._build_bili_support_reply_hint(events[-max_events:])
        auxiliary_context = self._build_bili_live_auxiliary_context(events[-max_events:])
        if auxiliary_context:
            prompt += "\n\n" + auxiliary_context
        if self.config.get("bili_live_auto_reply_force_full_tts", True):
            prompt += (
                "\n\n请只输出普通文本回复，不要调用工具，不要写 <record>、<voice>、"
                "<语音>、<send_message_to_user> 等标签；如果需要语音，系统 TTS 插件会自动处理。"
            )

        try:
            evt = copy.copy(self._bili_reply_event_template)
            evt.message_obj = copy.copy(self._bili_reply_event_template.message_obj)
            evt._extras = dict(self._bili_reply_event_template.get_extra())
            evt.clear_result()
            evt.message_obj.message = [Plain(prompt)]
            evt.message_obj.message_str = prompt
            evt.message_str = prompt
            evt.is_at_or_wake_command = True
            evt.should_call_llm(True)
            evt.set_extra("bili_live_auto_reply", True)
            evt.set_extra("bili_live_events", [event.raw for event in events[-max_events:]])
            self.context.get_event_queue().put_nowait(evt)
            self._bili_last_auto_reply_at = time.time()
            self._record_bili_auto_reply_rate_mark(events[-max_events:])
            logger.info(
                f"[B站直播] 已投递原生自动回应事件 -> {session_id}: {len(events[-max_events:])} 条事件"
            )
        except Exception as e:
            logger.warning(f"[B站直播] 投递原生自动回应事件失败: {e}")

    async def _reply_to_bili_live_events_via_framework(
        self, events: list[LiveDanmakuEvent], session_id: str
    ) -> bool:
        started_at = time.perf_counter()
        max_events = max(
            1,
            self._safe_parse_int(self.config.get("bili_live_auto_reply_max_events"), 5),
        )
        formatted = self._format_bili_events(events[-max_events:])
        if not formatted:
            return False

        try:
            session = MessageSession.from_str(session_id)
        except Exception as e:
            logger.warning(f"[B站直播] 无法解析自动回应会话: {session_id} err={e}")
            return False

        try:
            t_conv = time.perf_counter()
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(session_id)
            if not curr_cid:
                curr_cid = await self.context.conversation_manager.new_conversation(
                    session_id,
                    title="B站直播自动回应",
                )
                logger.info(f"[B站直播] 已为自动回应会话创建对话: {session_id}")
            conv = await self.context.conversation_manager.get_conversation(session_id, curr_cid)
            if not conv:
                logger.warning(f"[B站直播] 自动回应会话无法读取对话: {session_id}")
                return False
            conv_elapsed = time.perf_counter() - t_conv
        except Exception as e:
            logger.warning(f"[B站直播] 读取自动回应会话对话失败: {e}")
            return False

        prompt = (
            "【B站直播间弹幕事件】\n"
            "请像正在直播中收到弹幕一样回应直播间观众。\n"
            "身份边界：下面的用户名是 B站直播间观众昵称，不是当前私聊对象，也不等于私聊历史里的用户或群友；"
            "不要把私聊记忆、旧对话人物、现实称呼代入当前弹幕。\n"
            "要求：自然回应，不要逐条复读；优先回应具体问题或反馈；不要说自己看不到弹幕；"
            "如果弹幕只是“摸摸/贴贴/抱抱”这类互动，就直接以主播身份回应这个观众，"
            "不要说第三个人在摸你，也不要提无关照片、日程或旧聊天。\n"
            "只输出要发给直播间观众的话，不要描述发送状态、处理过程或自己的回应策略。\n\n"
            f"{formatted}"
        )
        prompt += self._build_bili_support_reply_hint(events[-max_events:])
        auxiliary_context = self._build_bili_live_auxiliary_context(events[-max_events:])
        if auxiliary_context:
            prompt += "\n\n" + auxiliary_context
        if self.config.get("bili_live_auto_reply_force_full_tts", True):
            prompt += (
                "\n\n请只输出普通文本回复，不要调用工具，不要写 <record>、<voice>、"
                "<语音>、<send_message_to_user> 等标签；如果需要语音，系统 TTS 插件会自动处理。"
            )

        try:
            synthetic_event = SyntheticBiliLiveWakeEvent(
                template_event=self._bili_reply_event_template,
                context=self.context,
                session=session,
                message="bili_live_auto_reply_wakeup",
            )
            synthetic_event.set_extra("bili_live_auto_reply", True)
            synthetic_event.set_extra(
                "bili_live_events", [event.raw for event in events[-max_events:]]
            )
            cfg = self.context.get_config(umo=session_id)
            provider_settings = cfg.get("provider_settings", {}) if isinstance(cfg, dict) else {}
            build_cfg = MainAgentBuildConfig(
                tool_call_timeout=int(provider_settings.get("tool_call_timeout", 120) or 120),
                llm_safety_mode=False,
                streaming_response=False,
            )
            req = ProviderRequest(
                prompt=prompt,
                conversation=conv,
                session_id=session_id,
            )
            t_build = time.perf_counter()
            result = await build_main_agent(
                event=synthetic_event,
                plugin_context=self.context,
                config=build_cfg,
                req=req,
            )
            if not result:
                return False
            build_elapsed = time.perf_counter() - t_build
            runner = result.agent_runner
            t_llm = time.perf_counter()
            async for _ in runner.step_until_done(20):
                pass
            llm_elapsed = time.perf_counter() - t_llm
            llm_resp = runner.get_final_llm_resp()
            if not llm_resp or llm_resp.role != "assistant":
                return False
            reply_text = self._clean_auto_reply_text(llm_resp.completion_text or "")
            if not reply_text:
                return False
            t_decorate = time.perf_counter()
            force_voice = bool(self.config.get("bili_live_auto_reply_force_full_tts", True))
            chain = await self._decorate_bili_live_reply_chain(
                session_id,
                [Plain(reply_text)],
                force_voice=False,
                skip_subtitle=force_voice,
            )
            decorate_elapsed = time.perf_counter() - t_decorate
            chain = self._strip_tts_blocks_from_plain_chain(chain)
            chain = self._ensure_visible_text_after_voice(chain, reply_text)
            t_send = time.perf_counter()
            await self.context.send_message(session_id, MessageChain(chain))
            send_elapsed = time.perf_counter() - t_send
            self._bili_last_auto_reply_at = time.time()
            self._record_bili_auto_reply_rate_mark(events[-max_events:])
            await self._push_subtitle(reply_text, source="bili_live")
            if force_voice:
                asyncio.create_task(
                    self._send_bili_live_tts_followup(
                        session_id,
                        reply_text,
                        push_subtitle=False,
                    )
                )
            total_elapsed = time.perf_counter() - started_at
            logger.info(
                "[B站直播] 自动回应耗时: total=%.2fs conv=%.2fs build=%.2fs llm=%.2fs decorate_tts=%.2fs send=%.2fs session=%s",
                total_elapsed,
                conv_elapsed,
                build_elapsed,
                llm_elapsed,
                decorate_elapsed,
                send_elapsed,
                session_id,
            )
            logger.info(f"[B站直播] 已通过完整框架链路自动回应弹幕 -> {session_id}: {reply_text}")
            return True
        except Exception as e:
            logger.warning(f"[B站直播] 框架式原生自动回应失败: {e}")
            return False

    async def _decorate_bili_live_reply_chain(
        self,
        session_id: str,
        chain: list[Any],
        force_voice: bool = False,
        skip_subtitle: bool = False,
    ) -> list[Any]:
        if not chain:
            return chain
        if force_voice:
            chain = self._wrap_plain_chain_as_tts(chain)
        try:
            session = MessageSession.from_str(session_id)
            message_obj = AstrBotMessage()
            message_obj.type = session.message_type
            message_obj.self_id = session.session_id
            message_obj.session_id = session.session_id
            message_obj.message_id = f"bili_live_reply_{uuid.uuid4().hex}"
            message_obj.sender = MessageMember(user_id=session.session_id)
            message_obj.message = chain
            message_obj.message_str = ""
            message_obj.raw_message = None
            message_obj.timestamp = int(time.time())
            platform_meta = None
            if self._bili_reply_event_template:
                try:
                    platform_meta = self._bili_reply_event_template.get_platform_metadata()
                except Exception:
                    platform_meta = None
            if platform_meta is None:
                platform_meta = PlatformMetadata(
                    name=session.platform_id,
                    description="SyntheticBiliLiveReply",
                    id=session.platform_id,
            )
            event = AstrMessageEvent("", message_obj, platform_meta, message_obj.session_id)
            if skip_subtitle:
                event.set_extra("bili_live_skip_subtitle", True)
            event.set_result(self._build_message_result_from_chain(chain))
        except Exception as e:
            logger.debug(f"[B站直播] 构造自动回应装饰事件失败，跳过 hooks: {e}")
            return chain

        try:
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.OnDecoratingResultEvent
            )
        except Exception as e:
            logger.debug(f"[B站直播] 获取装饰 hooks 失败: {e}")
            return chain
        if force_voice:
            self._mark_tts_modify_forced_voice(event, handlers)
        for handler in handlers:
            try:
                await handler.handler(event)
            except Exception as e:
                logger.warning(
                    "[B站直播] 自动回应装饰 hook 失败: %s: %s",
                    getattr(handler, "handler_full_name", "unknown"),
                    e,
                )
        result = event.get_result()
        processed = getattr(result, "chain", None) if result is not None else None
        processed_chain = list(processed or chain)
        if force_voice and not any(isinstance(component, Record) for component in processed_chain):
            spoken = self._plain_chain_text(processed_chain)
            record_chain = await self._build_bili_live_tts_chain(session_id, spoken)
            if record_chain:
                return record_chain
        return processed_chain

    async def _send_bili_live_tts_followup(
        self,
        session_id: str,
        text: str,
        *,
        push_subtitle: bool = True,
    ) -> None:
        started_at = time.perf_counter()
        try:
            record_chain = await self._build_bili_live_tts_chain(
                session_id,
                text,
                push_subtitle=push_subtitle,
            )
            if not record_chain:
                return
            await self.context.send_message(session_id, MessageChain(record_chain))
            logger.info(
                "[B站直播] 已后台补发直播自动回应 TTS: elapsed=%.2fs session=%s",
                time.perf_counter() - started_at,
                session_id,
            )
        except Exception as e:
            logger.warning("[B站直播] 后台补发直播自动回应 TTS 失败: %s", e)

    def _wrap_plain_chain_as_tts(self, chain: list[Any]) -> list[Any]:
        wrapped: list[Any] = []
        for component in chain:
            if isinstance(component, Plain):
                text = str(getattr(component, "text", "") or "").strip()
                if text and "<tts" not in text.lower():
                    wrapped.append(Plain(f"<tts>{text}</tts>"))
                    continue
            wrapped.append(component)
        return wrapped

    def _plain_chain_text(self, chain: list[Any]) -> str:
        parts: list[str] = []
        for component in chain:
            if isinstance(component, Plain):
                text = str(getattr(component, "text", "") or "").strip()
                text = self._strip_tts_blocks_from_text(text)
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()

    async def _build_bili_live_tts_chain(
        self,
        session_id: str,
        text: str,
        *,
        push_subtitle: bool = True,
    ) -> list[Any]:
        spoken = self._strip_tts_blocks_from_text(text)
        if not spoken:
            return []
        try:
            tts_provider = self.context.get_using_tts_provider(session_id)
        except Exception as e:
            logger.warning("[B站直播] 直播自动回应 TTS 生成失败：未找到会话 TTS Provider session=%s err=%s", session_id, e)
            return []
        if not tts_provider:
            logger.warning("[B站直播] 直播自动回应 TTS 生成失败：当前会话未配置 TTS Provider session=%s", session_id)
            return []
        convert_started_at = time.perf_counter()
        spoken = await self._convert_bili_live_tts_spoken_text(session_id, spoken, tts_provider)
        convert_elapsed = time.perf_counter() - convert_started_at
        if not spoken:
            return []
        try:
            tts_started_at = time.perf_counter()
            audio_path = await tts_provider.get_audio(spoken)
            tts_elapsed = time.perf_counter() - tts_started_at
        except Exception as e:
            logger.warning("[B站直播] 直播自动回应 TTS 生成失败: %s", e)
            return []
        if not audio_path:
            logger.warning("[B站直播] 直播自动回应 TTS 生成失败：Provider 未返回音频路径")
            return []
        try:
            record = Record(file=str(audio_path), url=str(audio_path))
        except TypeError:
            try:
                record = Record(file=str(audio_path))
            except TypeError:
                record = Record.fromFileSystem(str(audio_path))
        visible_text = self._strip_tts_blocks_from_text(text)
        subtitle_text = spoken if bool(self.config.get("subtitle_use_tts_spoken_text", False)) else visible_text
        subtitle_text = self._strip_tts_blocks_from_text(subtitle_text) or visible_text
        asyncio.create_task(
            self._after_bili_live_tts_audio_generated(
                str(audio_path),
                spoken,
                subtitle_text=subtitle_text,
            )
        )
        if push_subtitle and not self._companion_tts_live_subtitle_enabled():
            await self._push_subtitle(subtitle_text, source="bili_live")
        logger.info(
            "[B站直播] 已生成直播自动回应 TTS: convert=%.2fs provider=%.2fs path=%s text=%s",
            convert_elapsed,
            tts_elapsed,
            audio_path,
            spoken[:80],
        )
        return [record]

    async def _convert_bili_live_tts_spoken_text(
        self, session_id: str, text: str, tts_provider: Any
    ) -> str:
        companion = self._get_private_companion_plugin()
        if companion is None or not getattr(companion, "enable_tts_enhancement", False):
            return text
        convert = getattr(companion, "_convert_text_to_tts_markup", None)
        normalize_spoken = getattr(companion, "_normalize_tts_spoken_text", None)
        provider_kind_getter = getattr(companion, "_tts_provider_kind", None)
        if not callable(convert) or not callable(normalize_spoken):
            return text
        event = SimpleNamespace(
            unified_msg_origin=session_id,
            message_str=text,
            message_obj=SimpleNamespace(message=[]),
            get_sender_id=lambda: "",
        )
        try:
            converted = await convert(text, event, full=True)
            provider_kind = (
                provider_kind_getter(tts_provider=tts_provider)
                if callable(provider_kind_getter)
                else "generic"
            )
            spoken = normalize_spoken(converted, provider_kind=provider_kind)
            return self._strip_tts_blocks_from_text(spoken) or text
        except Exception as e:
            logger.warning("[B站直播] 调用陪伴插件 TTS 文本转换失败，使用原文: %s", e)
            return text

    def _companion_tts_live_subtitle_enabled(self) -> bool:
        companion = self._get_private_companion_plugin()
        return bool(
            companion is not None
            and getattr(companion, "enable_tts_live_subtitle_sync", False)
        )

    async def _after_bili_live_tts_audio_generated(
        self,
        audio_path: str,
        spoken_text: str,
        *,
        subtitle_text: str = "",
    ) -> None:
        plugin = self._get_private_companion_plugin()
        after_tts = getattr(plugin, "_after_tts_audio_generated", None) if plugin is not None else None
        if not callable(after_tts):
            return
        try:
            try:
                await after_tts(
                    audio_path,
                    spoken_text,
                    source="bili_live_auto_reply",
                    subtitle_text=subtitle_text,
                )
            except TypeError:
                await after_tts(audio_path, spoken_text)
        except Exception as e:
            logger.warning("[B站直播] 调用陪伴插件 TTS 本机联动失败: %s", e)

    def _mark_tts_modify_forced_voice(self, event: AstrMessageEvent, handlers: list[Any]) -> None:
        for handler in handlers:
            owner = getattr(getattr(handler, "handler", None), "__self__", None)
            if owner is None:
                continue
            mark_llm = getattr(owner, "_mark_pending_llm_response_event", None)
            mark_voice = getattr(owner, "_mark_pending_forced_voice_event", None)
            if not callable(mark_llm) or not callable(mark_voice):
                continue
            try:
                mark_llm(event)
                mark_voice(event)
                logger.debug("[B站直播] 已为自动回应标记 TTS 强制语音。")
                return
            except Exception as e:
                logger.debug(f"[B站直播] 标记 TTS 强制语音失败: {e}")
                return

    def _build_message_result_from_chain(self, chain: list[Any]) -> Any:
        try:
            from astrbot.api.event import MessageEventResult
        except ImportError:
            from astrbot.core.message.message_event_result import MessageEventResult
        try:
            result = MessageEventResult(chain=chain)
        except TypeError:
            result = MessageEventResult().chain_result(chain)
        if hasattr(result, "use_t2i"):
            try:
                result = result.use_t2i(False)
            except Exception:
                pass
        elif hasattr(result, "use_t2i_"):
            try:
                result.use_t2i_ = False
            except Exception:
                pass
        return result

    def _extract_provider_text(self, response) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response.strip()
        for attr in ("completion_text", "content", "text", "message"):
            value = getattr(response, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return str(response).strip()

    def _clean_auto_reply_text(self, text: str) -> str:
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip().strip('"“”')
        cleaned = self._strip_bili_reply_control_markup(cleaned)
        cleaned = self._strip_bili_meta_reply_lines(cleaned)
        max_length = self._safe_parse_int(
            self.config.get("bili_live_auto_reply_max_length"), 80
        )
        if max_length > 0 and len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip() + "..."
        return cleaned

    def _strip_bili_meta_reply_lines(self, text: str) -> str:
        lines = [line.strip() for line in str(text or "").splitlines()]
        kept: list[str] = []
        for line in lines:
            if not line:
                continue
            if self._is_bili_meta_reply_line(line):
                continue
            kept.append(line)
        return "\n".join(kept).strip()

    def _strip_bili_reply_control_markup(self, text: str) -> str:
        cleaned = str(text or "")
        if not cleaned:
            return ""

        cleaned = re.sub(
            r"(?is)<\s*(send_message_to_user|astrbot_execute_shell|astrbot_execute_python)\b.*$",
            "",
            cleaned,
        )
        cleaned = re.sub(r"(?is)<\s*message\s*>(.*?)<\s*/\s*message\s*>", r"\1", cleaned)
        cleaned = re.sub(
            r"(?is)<\s*(record|voice|tts|\u8bed\u97f3)\b[^>]*>(.*?)<\s*/\s*\1\s*>",
            r"\2",
            cleaned,
        )
        cleaned = re.sub(r"(?is)<\s*/?\s*(record|voice|tts|\u8bed\u97f3|message)\b[^>]*>", "", cleaned)
        cleaned = re.sub(r"(?is)<\s*/?\s*parameter\b[^>]*>", "", cleaned)
        cleaned = re.sub(r"(?is)<[^>\n]{1,120}>", "", cleaned)
        cleaned = re.sub(r"\[语音\]|\[voice\]|\[record\]", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _is_bili_meta_reply_line(self, line: str) -> bool:
        compact = re.sub(r"\s+", "", str(line or ""))
        if not compact:
            return True
        meta_patterns = (
            "消息已经发出",
            "消息已发出",
            "已经发出去了",
            "已经发送",
            "已发送",
            "我已经回应",
            "我刚刚回应",
            "温柔地回应",
            "希望没有冷落",
            "不要冷落",
            "处理了这条弹幕",
            "这条弹幕我没太看懂",
            "这条弹幕我没有太看懂",
            "弹幕我没太看懂",
            "弹幕我没有太看懂",
        )
        return any(pattern in compact for pattern in meta_patterns)

    def _strip_tts_blocks_from_plain_chain(self, chain: list[Any]) -> list[Any]:
        cleaned_chain: list[Any] = []
        for component in chain:
            if isinstance(component, Plain):
                text = self._strip_tts_blocks_from_text(getattr(component, "text", "") or "")
                text = self._dedupe_repeated_plain_text(text)
                if text:
                    component.text = text
                    cleaned_chain.append(component)
                continue
            cleaned_chain.append(component)
        return cleaned_chain or chain

    def _strip_tts_blocks_from_text(self, text: str) -> str:
        cleaned = self._strip_bili_reply_control_markup(str(text or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _dedupe_repeated_plain_text(self, text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        compact = re.sub(r"\s+", "", cleaned)
        if len(compact) % 2 == 0:
            half = len(compact) // 2
            if compact[:half] == compact[half:]:
                return cleaned[: max(1, len(cleaned) // 2)].strip()
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if len(lines) == 2 and lines[0] == lines[1]:
            return lines[0]
        return cleaned

    def _ensure_visible_text_after_voice(self, chain: list[Any], reply_text: str) -> list[Any]:
        visible_text = self._dedupe_repeated_plain_text(self._strip_tts_blocks_from_text(reply_text))
        if not visible_text or not any(isinstance(component, Record) for component in chain):
            return chain
        existing_plain = [
            self._dedupe_repeated_plain_text(
                self._strip_tts_blocks_from_text(getattr(component, "text", "") or "")
            )
            for component in chain
            if isinstance(component, Plain)
        ]
        if any(text == visible_text for text in existing_plain if text):
            return chain
        return [*chain, Plain(visible_text)]

    def _is_bili_live_running(self) -> bool:
        return bool(self._bili_live_task and not self._bili_live_task.done())

    def _get_bili_live_task_error(self) -> str:
        if not self._bili_live_task or not self._bili_live_task.done():
            return ""
        try:
            exc = self._bili_live_task.exception()
        except asyncio.CancelledError:
            return "任务已取消"
        except Exception as e:
            return str(e)
        return str(exc) if exc else ""

    def _recent_bili_events(
        self,
        limit: Optional[int] = None,
        include_events: Optional[list[str]] = None,
    ) -> list[LiveDanmakuEvent]:
        if limit is None:
            limit = int(self.config.get("bili_live_inject_max_events", 8) or 8)
        limit = max(1, limit)
        allowed = {item.strip() for item in include_events or [] if str(item).strip()}
        events = list(self._bili_events)
        if allowed:
            events = [event for event in events if event.event_type in allowed]
        return events[-limit:]

    def _format_bili_events(self, events: list[LiveDanmakuEvent]) -> str:
        if not events:
            return ""
        now = time.time()
        lines: list[str] = []
        for event in events:
            age = max(0, int(now - event.ts))
            lines.append(f"- [{event.event_type}，{age}秒前] {event.display_text()}")
        return "\n".join(lines)

    @staticmethod
    def _single_line_text(value: Any, limit: int = 120) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if limit > 0 and len(text) > limit:
            text = text[:limit].rstrip() + "..."
        return text

    def _private_companion_enabled(self) -> bool:
        return bool(self.config.get("private_companion_live_context_enabled", True))

    def _get_private_companion_plugin(self) -> Any | None:
        try:
            module = importlib.import_module(
                "data.plugins.astrbot_plugin_private_companion.main"
            )
            get_api = getattr(module, "get_private_companion_api", None)
            api = get_api() if callable(get_api) else None
            plugin = getattr(api, "_plugin", None)
            if plugin is not None:
                return plugin
        except Exception as e:
            logger.debug(f"[B站直播] 读取陪伴插件运行实例失败: {e}")
        return None

    @staticmethod
    def _private_companion_name_tokens(profile: dict[str, Any]) -> list[str]:
        tokens: list[str] = []
        for key in ("name", "nickname", "display_name"):
            value = str(profile.get(key) or "").strip()
            if value:
                tokens.append(value)
        for key in ("aliases", "observed_names"):
            values = profile.get(key)
            if isinstance(values, list):
                tokens.extend(str(item).strip() for item in values if str(item).strip())
        return list(dict.fromkeys(tokens))

    def _match_private_companion_member(
        self, plugin: Any, live_username: str, event: LiveDanmakuEvent | None = None
    ) -> dict[str, Any] | None:
        name = self._single_line_text(live_username, 60)
        if not name:
            return None
        external_ids = self._private_companion_live_external_ids(live_username, event)

        data = getattr(plugin, "data", None)
        profiles = data.get("worldbook_member_profiles") if isinstance(data, dict) else None
        if isinstance(profiles, dict):
            for user_id, profile in profiles.items():
                if not isinstance(profile, dict) or not profile.get("enabled", True):
                    continue
                profile_ids = {str(user_id)}
                raw_external = profile.get("external_ids")
                if isinstance(raw_external, list):
                    profile_ids.update(str(item).strip() for item in raw_external if str(item).strip())
                for key in ("linked_bili_profile_id", "live_profile_id"):
                    value = str(profile.get(key) or "").strip()
                    if value:
                        profile_ids.add(value)
                if external_ids & profile_ids:
                    return self._private_companion_linked_match(plugin, str(user_id), profile)

        resolver = getattr(plugin, "_resolve_worldbook_member_by_name", None)
        if callable(resolver):
            try:
                matches = resolver(name)
                if isinstance(matches, list) and matches:
                    return self._augment_private_companion_match(plugin, dict(matches[0]))
            except Exception as e:
                logger.debug(f"[B站直播] 调用陪伴插件关系网匹配失败: {e}")

        if not isinstance(profiles, dict):
            return None
        name_lower = name.lower()
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for user_id, profile in profiles.items():
            if not isinstance(profile, dict) or not profile.get("enabled", True):
                continue
            tokens = self._private_companion_name_tokens(profile)
            lowered = [token.lower() for token in tokens if token]
            if name_lower in lowered:
                rank = 0
            elif any(
                token and (name_lower in token or token in name_lower)
                for token in lowered
            ):
                rank = 1
            else:
                continue
            candidates.append((rank, str(user_id), profile))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        _rank, user_id, profile = candidates[0]
        return self._augment_private_companion_match(plugin, {
            "user_id": user_id,
            "name": self._single_line_text(profile.get("name"), 60) or user_id,
            "aliases": [
                self._single_line_text(item, 40)
                for item in profile.get("aliases", [])
                if self._single_line_text(item, 40)
            ][:8],
            "observed_names": [
                self._single_line_text(item, 40)
                for item in profile.get("observed_names", [])
                if self._single_line_text(item, 40)
            ][:8],
            "identity_note": self._single_line_text(
                profile.get("identity_note") or profile.get("note") or profile.get("content"),
                120,
            ),
            "source": "worldbook",
        })

    def _private_companion_live_external_ids(
        self, live_username: str, event: LiveDanmakuEvent | None = None
    ) -> set[str]:
        username = self._single_line_text(live_username, 60)
        ids = {f"live:{username}"} if username else set()
        raw = getattr(event, "raw", None) if event is not None else None
        uid = self._bili_event_uid(raw)
        if uid:
            ids.add(f"bili:{uid}")
        return ids

    def _private_companion_linked_match(
        self, plugin: Any, user_id: str, profile: dict[str, Any]
    ) -> dict[str, Any]:
        linked_id = str(profile.get("linked_qq_user_id") or profile.get("merged_into_user_id") or "").strip()
        if linked_id:
            linked_profile = self._private_companion_profile_by_user_id(plugin, linked_id)
            if isinstance(linked_profile, dict) and linked_profile.get("enabled", True):
                return self._augment_private_companion_match(plugin, {
                    "user_id": linked_id,
                    "name": self._single_line_text(linked_profile.get("name"), 60) or linked_id,
                    "source": "worldbook_external_bind",
                })
        return self._augment_private_companion_match(plugin, {
            "user_id": user_id,
            "name": self._single_line_text(profile.get("name"), 60) or user_id,
            "source": "worldbook_external",
        })

    @staticmethod
    def _bili_event_uid(raw: Any) -> str:
        if not isinstance(raw, dict):
            return ""
        candidates: list[Any] = [
            raw.get("uid"),
            raw.get("user_id"),
            raw.get("mid"),
        ]
        for key in ("user", "user_info", "info", "data"):
            value = raw.get(key)
            if isinstance(value, dict):
                candidates.extend([value.get("uid"), value.get("user_id"), value.get("mid")])
            elif isinstance(value, list):
                for item in value[:4]:
                    if isinstance(item, dict):
                        candidates.extend([item.get("uid"), item.get("user_id"), item.get("mid")])
                    elif isinstance(item, (int, str)):
                        candidates.append(item)
        for value in candidates:
            text = str(value or "").strip()
            if text.isdigit() and int(text) > 0:
                return text
        return ""

    def _private_companion_profile_by_user_id(
        self, plugin: Any, user_id: str
    ) -> dict[str, Any] | None:
        data = getattr(plugin, "data", None)
        profiles = data.get("worldbook_member_profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, dict):
            return None
        profile = profiles.get(str(user_id or ""))
        return profile if isinstance(profile, dict) else None

    def _augment_private_companion_match(
        self, plugin: Any, match: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = str(match.get("user_id") or "").strip()
        profile = self._private_companion_profile_by_user_id(plugin, user_id)
        if not isinstance(profile, dict):
            return match
        match.setdefault("name", self._single_line_text(profile.get("name"), 60) or user_id)
        match.setdefault("aliases", [
            self._single_line_text(item, 40)
            for item in profile.get("aliases", [])
            if self._single_line_text(item, 40)
        ][:8])
        match.setdefault("observed_names", [
            self._single_line_text(item, 40)
            for item in profile.get("observed_names", [])
            if self._single_line_text(item, 40)
        ][:8])
        if not match.get("identity_note"):
            match["identity_note"] = self._single_line_text(
                profile.get("identity_note") or profile.get("note") or profile.get("content"),
                120,
            )
        match["boundary_note"] = self._single_line_text(profile.get("boundary_note"), 140)
        match["profile_content"] = self._single_line_text(profile.get("content"), 140)
        memories = profile.get("important_memories")
        if isinstance(memories, list):
            memory_lines: list[str] = []
            for item in memories:
                if not isinstance(item, dict) or not item.get("enabled", True):
                    continue
                title = self._single_line_text(item.get("title"), 30)
                content = self._single_line_text(item.get("content"), 90)
                if content:
                    memory_lines.append(f"{title + ': ' if title else ''}{content}")
                if len(memory_lines) >= 2:
                    break
            match["important_memory_lines"] = memory_lines
        return match

    def _recent_private_companion_group_messages(
        self, plugin: Any, user_id: str
    ) -> list[dict[str, Any]]:
        data = getattr(plugin, "data", None)
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, dict):
            return []
        now = time.time()
        max_age = max(
            30.0,
            self._safe_parse_float(
                self.config.get("private_companion_live_context_max_age_seconds"),
                900.0,
            ),
        )
        limit = max(
            1,
            self._safe_parse_int(
                self.config.get("private_companion_live_context_recent_limit"),
                3,
            ),
        )
        rows: list[dict[str, Any]] = []
        for group_id, group in groups.items():
            if not isinstance(group, dict):
                continue
            group_name = self._private_companion_group_name(plugin, str(group_id), group)
            recent = group.get("recent_messages")
            if not isinstance(recent, list):
                continue
            for item in recent:
                if not isinstance(item, dict):
                    continue
                if str(item.get("sender_id") or "") != str(user_id):
                    continue
                ts = self._safe_parse_float(item.get("ts"), 0.0)
                if ts <= 0 or now - ts > max_age:
                    continue
                text = self._single_line_text(item.get("text"), 100)
                if not text:
                    continue
                rows.append(
                    {
                        "ts": ts,
                        "age": max(0, int(now - ts)),
                        "group_id": str(group_id),
                        "group_name": group_name,
                        "name": self._single_line_text(
                            item.get("identity_name") or item.get("name"), 40
                        ),
                        "text": text,
                    }
                )
        rows.sort(key=lambda item: item["ts"], reverse=True)
        return rows[:limit]

    def _private_companion_group_name(
        self, plugin: Any, group_id: str, group: dict[str, Any]
    ) -> str:
        data = getattr(plugin, "data", None)
        profiles = data.get("worldbook_group_profiles") if isinstance(data, dict) else None
        profile = profiles.get(group_id) if isinstance(profiles, dict) else None
        if isinstance(profile, dict):
            name = self._single_line_text(profile.get("name"), 40)
            if name:
                return name
        return (
            self._single_line_text(group.get("name") or group.get("group_name"), 40)
            or f"群 {group_id}"
        )

    def _build_private_companion_live_context(
        self, events: list[LiveDanmakuEvent]
    ) -> str:
        if not self._private_companion_enabled():
            return ""
        plugin = self._get_private_companion_plugin()
        if plugin is None:
            return ""
        lines: list[str] = []
        seen_users: set[str] = set()
        max_users = max(
            1,
            self._safe_parse_int(
                self.config.get("private_companion_live_context_max_users"),
                3,
            ),
        )
        for live_event in reversed(events):
            if live_event.event_type not in {
                "danmaku",
                "gift",
                "super_chat",
                "buy_guard",
                "enter_room",
                "follow",
                "like",
            }:
                continue
            match = self._match_private_companion_member(plugin, live_event.username, live_event)
            if not match:
                activity = self._private_companion_viewer_activity_for_context(
                    plugin, live_event.username
                )
                if activity and live_event.username not in seen_users:
                    seen_users.add(live_event.username)
                    lines.append(f"- 直播用户名 `{live_event.username}` 的活跃画像：{activity}")
                    if len(seen_users) >= max_users:
                        break
                continue
            user_id = str(match.get("user_id") or "").strip()
            if not user_id or user_id in seen_users:
                continue
            seen_users.add(user_id)
            display_name = self._single_line_text(match.get("name"), 40) or live_event.username
            alias_text = "、".join(
                item
                for item in [
                    *list(match.get("aliases") or []),
                    *list(match.get("observed_names") or []),
                ][:5]
                if item
            )
            group_messages = self._recent_private_companion_group_messages(plugin, user_id)
            activity = self._private_companion_viewer_activity_for_context(
                plugin, live_event.username, user_id=user_id
            )
            detail = f"- 直播用户名 `{live_event.username}` 可能对应关系网用户 `{display_name}`"
            if alias_text:
                detail += f"；可识别名称/别名：{alias_text}"
            style_hint = self._private_companion_relationship_style_hint(match)
            if style_hint:
                detail += f"；称呼/互动风格：{style_hint}"
            if self.config.get(
                "private_companion_live_context_include_identity_note", False
            ) and match.get("identity_note"):
                detail += f"；身份备注：{self._single_line_text(match.get('identity_note'), 80)}"
            lines.append(detail)
            if activity:
                lines.append(f"  - 直播活跃画像：{activity}")
            for msg in group_messages:
                lines.append(
                    f"  - {msg['age']}秒前在「{msg['group_name']}」说过：{msg['text']}"
                )
            if len(seen_users) >= max_users:
                break
        if not lines:
            return ""
        return (
            "## 跨场景观众线索\n"
            "以下线索来自“我会永远陪着你”的关系网与群聊观察。"
            "直播平台不提供 QQ 号，因此这是按直播用户名、关系网姓名、别名和观察名得到的候选匹配。"
            "可以在高置信时自然提一句“刚还在群里……”之类的跨场景寒暄；"
            "如果给出了称呼/互动风格，直播回复可按该风格称呼对方，但不要把风格说明原样说出；"
            "不要说出 QQ 号、内部关系网、匹配过程或隐私备注；不确定时就当普通观众回应。\n"
            + "\n".join(lines)
        )

    def _private_companion_relationship_style_hint(self, match: dict[str, Any]) -> str:
        if not self.config.get("private_companion_relationship_style_context_enabled", True):
            return ""
        name = self._single_line_text(match.get("name"), 30)
        aliases = [
            self._single_line_text(item, 24)
            for item in [
                *list(match.get("aliases") or []),
                *list(match.get("observed_names") or []),
            ]
            if self._single_line_text(item, 24)
        ]
        parts: list[str] = []
        if name:
            parts.append(f"可称呼为{name}")
        if aliases:
            parts.append("也认得这些称呼：" + "、".join(aliases[:4]))
        boundary = self._single_line_text(match.get("boundary_note"), 100)
        if boundary:
            parts.append(f"边界：{boundary}")
        content = self._single_line_text(match.get("profile_content"), 100)
        if content and self.config.get("private_companion_relationship_style_include_profile", False):
            parts.append(f"画像：{content}")
        memories = match.get("important_memory_lines")
        if (
            isinstance(memories, list)
            and memories
            and self.config.get("private_companion_relationship_style_include_memories", False)
        ):
            parts.append("相关记忆：" + "；".join(self._single_line_text(item, 80) for item in memories[:2]))
        return "；".join(parts)

    def _private_companion_viewer_activity_for_context(
        self, plugin: Any, live_username: str, *, user_id: str = ""
    ) -> str:
        if not self.config.get("private_companion_viewer_activity_context_enabled", True):
            return ""
        store = self._private_companion_live_state_store(plugin)
        activity_map = store.get("viewer_activity")
        if not isinstance(activity_map, dict):
            return ""
        keys = []
        if user_id:
            keys.append(f"user:{user_id}")
        if live_username:
            keys.append(f"live:{live_username}")
        item = None
        for key in keys:
            candidate = activity_map.get(key)
            if isinstance(candidate, dict):
                item = candidate
                break
        if not isinstance(item, dict):
            return ""
        total = self._safe_parse_int(item.get("total_events"), 0)
        if total <= 0:
            return ""
        event_counts = item.get("event_counts") if isinstance(item.get("event_counts"), dict) else {}
        highlights: list[str] = [f"累计互动 {total} 次"]
        danmaku_count = self._safe_parse_int(event_counts.get("danmaku"), 0)
        if danmaku_count:
            highlights.append(f"弹幕 {danmaku_count} 条")
        gift_count = sum(
            self._safe_parse_int(event_counts.get(kind), 0)
            for kind in ("gift", "super_chat", "buy_guard")
        )
        if gift_count:
            highlights.append(f"重要互动 {gift_count} 次")
        first_seen = self._safe_parse_float(item.get("first_seen"), 0)
        if first_seen:
            days = max(0, int((time.time() - first_seen) / 86400))
            if days >= 1:
                highlights.append(f"已出现约 {days} 天")
        recent_danmaku = item.get("recent_danmaku") if isinstance(item.get("recent_danmaku"), list) else []
        if recent_danmaku:
            samples = [
                self._single_line_text(row.get("content") if isinstance(row, dict) else row, 36)
                for row in recent_danmaku[:3]
            ]
            samples = [item for item in samples if item]
            if samples:
                highlights.append("最近常聊：" + " / ".join(samples))
        return "；".join(highlights)

    def _live_memory_enabled(self) -> bool:
        return bool(self.config.get("live_memory_enabled", True))

    def _live_memory_context_enabled(self) -> bool:
        return bool(self.config.get("live_memory_context_enabled", True))

    def _live_memory_highlight_event_types(self) -> set[str]:
        raw = self.config.get(
            "live_memory_highlight_event_types",
            ["gift", "super_chat", "buy_guard"],
        )
        if not isinstance(raw, list):
            raw = ["gift", "super_chat", "buy_guard"]
        return {str(item).strip() for item in raw if str(item).strip()}

    def _build_bili_live_auxiliary_context(
        self, events: list[LiveDanmakuEvent]
    ) -> str:
        parts = [
            self._build_bili_live_continuity_context(events),
            self._build_live_stream_memory_context(events),
            self._build_private_companion_live_context(events),
        ]
        return "\n\n".join(part for part in parts if part)

    def _build_bili_live_continuity_context(
        self, events: list[LiveDanmakuEvent]
    ) -> str:
        if not events:
            return ""
        session_events = [
            item
            for item in self._bili_session_events
            if item.event_type in {"danmaku", "gift", "super_chat", "buy_guard", "follow", "enter_room"}
        ]
        if len(session_events) <= len(events):
            return ""

        current_keys = {
            (item.event_type, item.username, item.content, int(item.ts * 1000))
            for item in events
        }
        current_names: list[str] = []
        for item in events:
            name = self._single_line_text(item.username, 40)
            if name and name != "系统" and name not in current_names:
                current_names.append(name)

        lines: list[str] = []
        max_viewers = max(
            1,
            self._safe_parse_int(
                self.config.get("bili_live_continuity_context_max_viewers"),
                3,
            ),
        )
        max_per_viewer = max(
            1,
            self._safe_parse_int(
                self.config.get("bili_live_continuity_context_per_viewer"),
                4,
            ),
        )
        now = time.time()
        for name in current_names[:max_viewers]:
            prior: list[LiveDanmakuEvent] = []
            for item in reversed(session_events):
                key = (item.event_type, item.username, item.content, int(item.ts * 1000))
                if key in current_keys:
                    continue
                if item.username != name:
                    continue
                if item.event_type not in {"danmaku", "gift", "super_chat", "buy_guard"}:
                    continue
                prior.append(item)
                if len(prior) >= max_per_viewer:
                    break
            if not prior:
                continue
            snippets = []
            for item in reversed(prior):
                age = max(0, int(now - item.ts))
                snippets.append(
                    f"{age}秒前{item.event_type}: {self._single_line_text(item.content, 48)}"
                )
            lines.append(f"- {name} 本场前文：" + "；".join(snippets))

        recent_room = []
        for item in reversed(session_events):
            key = (item.event_type, item.username, item.content, int(item.ts * 1000))
            if key in current_keys:
                continue
            if item.event_type != "danmaku":
                continue
            text = self._single_line_text(item.display_text(), 64)
            if text:
                recent_room.append(text)
            if len(recent_room) >= 5:
                break
        if recent_room:
            lines.append("- 直播间刚聊过：" + "；".join(reversed(recent_room)))

        if not lines:
            return ""
        return (
            "## 本场连续对话上下文\n"
            "下面是直播间本场已经发生过的近距离互动，用于承接同一观众的前文。"
            "如果当前弹幕像是在接着聊，请直接顺着前文回应；不要把连续发言当作观众刚来，"
            "也不要机械复述这些上下文。\n"
            + "\n".join(lines)
        )

    def _build_live_stream_memory_context(
        self, events: list[LiveDanmakuEvent]
    ) -> str:
        if not self._live_memory_context_enabled():
            return ""
        plugin = self._get_private_companion_plugin()
        if plugin is None:
            return ""
        store = self._private_companion_live_state_store(plugin)
        if not store:
            return ""

        max_lines = max(
            4,
            self._safe_parse_int(self.config.get("live_memory_context_max_lines"), 12),
        )
        lines: list[str] = []
        session_line = self._live_memory_session_line()
        if session_line:
            lines.append(f"- 本场状态：{session_line}")

        for item in self._live_memory_recent_items(store, limit=3):
            lines.append(f"- 可承接记忆：{item}")
            if len(lines) >= max_lines:
                break

        if len(lines) < max_lines:
            for item in self._live_memory_recent_highlights(store, limit=3):
                lines.append(f"- 最近高光：{item}")
                if len(lines) >= max_lines:
                    break

        if len(lines) < max_lines:
            topics = self._live_memory_topic_lines(store, limit=4)
            if topics:
                lines.append("- 常见话题：" + "；".join(topics))

        if len(lines) < max_lines:
            threads = self._live_memory_open_thread_lines(store, limit=3)
            if threads:
                lines.append("- 未完话题：" + "；".join(threads))

        if len(lines) < max_lines:
            for item in self._live_memory_viewer_lines(plugin, events, limit=3):
                lines.append(f"- 观众记忆：{item}")
                if len(lines) >= max_lines:
                    break

        if not lines:
            return ""
        return (
            "## 直播专用记忆上下文\n"
            "以下是专门为直播场景整理的记忆，只用于让回复更像连续直播互动。"
            "可以自然承接常聊话题、高光和未完梗；不要说出内部字段、存储位置或分析过程；"
            "没有把握时只当作轻量背景，不要强行认亲或编造事实。\n"
            + "\n".join(lines[:max_lines])
        )

    def _live_memory_session_line(self) -> str:
        events = list(self._bili_session_events)
        if not events:
            return ""
        counts: dict[str, int] = {}
        viewers: dict[str, int] = {}
        for event in events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
            if event.username and event.username != "系统":
                viewers[event.username] = viewers.get(event.username, 0) + 1
        top_viewers = sorted(viewers.items(), key=lambda item: item[1], reverse=True)[:3]
        viewer_text = "、".join(f"{name}({count})" for name, count in top_viewers)
        count_text = "、".join(f"{key}{value}" for key, value in counts.items())
        duration = max(
            1,
            int((time.time() - (self._bili_session_started_at or events[0].ts)) / 60),
        )
        parts = [f"已直播约 {duration} 分钟", f"本场互动 {len(events)} 条"]
        if count_text:
            parts.append(count_text)
        if viewer_text:
            parts.append("活跃观众：" + viewer_text)
        return "；".join(parts)

    def _live_memory_recent_items(self, store: dict[str, Any], limit: int) -> list[str]:
        items = store.get("memory_items")
        if not isinstance(items, list):
            return []
        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = self._single_line_text(item.get("text"), 120)
            if not text:
                continue
            username = self._single_line_text(item.get("username"), 30)
            if username:
                text = f"{username}：{text}"
            lines.append(text)
            if len(lines) >= limit:
                break
        return lines

    def _live_memory_recent_highlights(
        self, store: dict[str, Any], limit: int
    ) -> list[str]:
        items = store.get("highlight_events")
        if not isinstance(items, list):
            return []
        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = self._single_line_text(item.get("text"), 120)
            if not text:
                continue
            lines.append(text)
            if len(lines) >= limit:
                break
        return lines

    def _live_memory_topic_lines(
        self, store: dict[str, Any], limit: int
    ) -> list[str]:
        topics = store.get("topic_memory")
        if not isinstance(topics, dict):
            return []
        rows = []
        for topic, item in topics.items():
            if not isinstance(item, dict):
                continue
            rows.append(
                (
                    self._safe_parse_int(item.get("count"), 0),
                    self._safe_parse_float(item.get("last_seen"), 0.0),
                    str(topic),
                    item,
                )
            )
        rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
        lines: list[str] = []
        for count, _ts, topic, item in rows[:limit]:
            samples = item.get("samples") if isinstance(item.get("samples"), list) else []
            sample_text = ""
            for sample in samples[:1]:
                if isinstance(sample, dict):
                    sample_text = self._single_line_text(sample.get("text"), 42)
                else:
                    sample_text = self._single_line_text(sample, 42)
                if sample_text:
                    break
            detail = f"{topic}({count}次)"
            if sample_text:
                detail += f" 最近：{sample_text}"
            lines.append(detail)
        return lines

    def _live_memory_open_thread_lines(
        self, store: dict[str, Any], limit: int
    ) -> list[str]:
        items = store.get("open_threads")
        if not isinstance(items, list):
            return []
        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = self._single_line_text(item.get("text"), 90)
            username = self._single_line_text(item.get("username"), 30)
            if not text:
                continue
            lines.append(f"{username + '：' if username else ''}{text}")
            if len(lines) >= limit:
                break
        return lines

    def _live_memory_viewer_lines(
        self, plugin: Any, events: list[LiveDanmakuEvent], limit: int
    ) -> list[str]:
        lines: list[str] = []
        seen: set[str] = set()
        for live_event in reversed(events):
            username = self._single_line_text(live_event.username, 40)
            if not username or username in seen or username in {"系统"}:
                continue
            seen.add(username)
            match = self._match_private_companion_member(plugin, username, live_event)
            user_id = str((match or {}).get("user_id") or "").strip()
            activity = self._private_companion_viewer_activity_for_context(
                plugin, username, user_id=user_id
            )
            if activity:
                name = self._single_line_text((match or {}).get("name"), 30) or username
                lines.append(f"{name}：{activity}")
            if len(lines) >= limit:
                break
        return lines

    def _format_live_memory_overview(
        self, plugin: Any, limit: int = 8
    ) -> str:
        store = self._private_companion_live_state_store(plugin)
        if not store:
            return ""
        limit = max(1, min(30, limit))
        sections: list[str] = []
        session_line = self._live_memory_session_line()
        if session_line:
            sections.append("本场直播：" + session_line)
        items = self._live_memory_recent_items(store, limit=limit)
        if items:
            sections.append("可承接记忆：\n" + "\n".join(f"- {item}" for item in items))
        highlights = self._live_memory_recent_highlights(store, limit=min(5, limit))
        if highlights:
            sections.append("最近高光：\n" + "\n".join(f"- {item}" for item in highlights))
        topics = self._live_memory_topic_lines(store, limit=min(8, limit))
        if topics:
            sections.append("常见话题：\n" + "\n".join(f"- {item}" for item in topics))
        threads = self._live_memory_open_thread_lines(store, limit=min(5, limit))
        if threads:
            sections.append("未完话题：\n" + "\n".join(f"- {item}" for item in threads))
        summaries = store.get("summaries") if isinstance(store.get("summaries"), list) else []
        summary_lines = []
        for item in reversed(summaries[-min(3, limit):]):
            if isinstance(item, dict):
                text = self._single_line_text(item.get("summary") or item.get("body"), 140)
                if text:
                    summary_lines.append(text)
        if summary_lines:
            sections.append("最近直播小结：\n" + "\n".join(f"- {item}" for item in summary_lines))
        return "\n\n".join(sections)

    def _private_companion_writeback_enabled(self) -> bool:
        return bool(self.config.get("private_companion_writeback_enabled", True))

    def _private_companion_writeback_event_types(self) -> set[str]:
        raw = self.config.get(
            "private_companion_writeback_memory_event_types",
            ["gift", "super_chat", "buy_guard"],
        )
        if not isinstance(raw, list):
            raw = ["gift", "super_chat", "buy_guard"]
        return {str(item).strip() for item in raw if str(item).strip()}

    def _private_companion_event_key(self, event: LiveDanmakuEvent) -> str:
        bucket = int((event.ts or time.time()) // 10)
        return f"{event.event_type}|{event.username}|{event.content}|{bucket}"

    def _private_companion_live_state_store(self, plugin: Any) -> dict[str, Any]:
        data = getattr(plugin, "data", None)
        if not isinstance(data, dict):
            return {}
        store = data.setdefault("live_stream_companion", {})
        if not isinstance(store, dict):
            store = {}
            data["live_stream_companion"] = store
        store.setdefault("viewer_observations", {})
        store.setdefault("viewer_activity", {})
        store.setdefault("summaries", [])
        store.setdefault("memory_items", [])
        store.setdefault("topic_memory", {})
        store.setdefault("highlight_events", [])
        store.setdefault("open_threads", [])
        store.setdefault("stream_profile", {})
        return store

    async def _write_private_companion_live_event(self, event: LiveDanmakuEvent) -> None:
        if not (
            self._private_companion_writeback_enabled()
            or self._live_memory_enabled()
        ):
            return
        plugin = self._get_private_companion_plugin()
        if plugin is None:
            return

        event_key = self._private_companion_event_key(event)
        if event_key in self._private_companion_writeback_seen:
            return
        self._private_companion_writeback_seen.add(event_key)

        try:
            lock = getattr(plugin, "_data_lock", None)
            if lock is not None:
                async with lock:
                    changed = self._write_private_companion_live_event_locked(
                        plugin, event, event_key
                    )
                    if changed:
                        self._save_private_companion(plugin)
            else:
                changed = self._write_private_companion_live_event_locked(
                    plugin, event, event_key
                )
                if changed:
                    self._save_private_companion(plugin)
        except Exception as e:
            logger.debug(f"[B站直播] 写回陪伴插件直播事件失败: {e}")

    def _write_private_companion_live_event_locked(
        self, plugin: Any, event: LiveDanmakuEvent, event_key: str
    ) -> bool:
        changed = False
        match = self._match_private_companion_member(plugin, event.username, event)
        writeback_enabled = self._private_companion_writeback_enabled()
        if self.config.get("private_companion_viewer_activity_enabled", True):
            changed = self._record_private_companion_viewer_activity(
                plugin, event, match
            ) or changed
        if self._live_memory_enabled():
            changed = self._record_private_companion_live_memory(
                plugin, event, match
            ) or changed
        if not writeback_enabled:
            return changed
        if match:
            changed = self._write_private_companion_viewer_memory(
                plugin, match, event, event_key
            ) or changed
        elif self.config.get("private_companion_auto_register_viewers", True):
            changed = self._maybe_register_private_companion_live_viewer(
                plugin, event
            ) or changed

        if self.config.get("private_companion_live_state_enabled", True):
            changed = self._maybe_apply_private_companion_live_state(plugin, event) or changed
        return changed

    def _record_private_companion_viewer_activity(
        self,
        plugin: Any,
        event: LiveDanmakuEvent,
        match: dict[str, Any] | None = None,
    ) -> bool:
        if event.event_type not in {
            "danmaku",
            "gift",
            "super_chat",
            "buy_guard",
            "enter_room",
            "follow",
            "like",
        }:
            return False
        username = self._single_line_text(event.username, 40)
        if not username or username in {"系统"}:
            return False
        store = self._private_companion_live_state_store(plugin)
        activity_map = store.setdefault("viewer_activity", {})
        if not isinstance(activity_map, dict):
            activity_map = {}
            store["viewer_activity"] = activity_map
        user_id = str((match or {}).get("user_id") or "").strip()
        display_name = self._single_line_text((match or {}).get("name"), 40) or username
        primary_key = f"user:{user_id}" if user_id else f"live:{username}"
        item = activity_map.setdefault(
            primary_key,
            {
                "viewer_key": primary_key,
                "live_username": username,
                "user_id": user_id,
                "display_name": display_name,
                "first_seen": time.time(),
                "last_seen": 0,
                "total_events": 0,
                "event_counts": {},
                "recent_events": [],
                "recent_danmaku": [],
            },
        )
        if not isinstance(item, dict):
            item = {"viewer_key": primary_key, "recent_events": [], "recent_danmaku": []}
            activity_map[primary_key] = item
        item["viewer_key"] = primary_key
        item["live_username"] = username
        item["user_id"] = user_id
        item["display_name"] = display_name
        item.setdefault("first_seen", time.time())
        item["last_seen"] = time.time()
        item["total_events"] = self._safe_parse_int(item.get("total_events"), 0) + 1
        event_counts = item.setdefault("event_counts", {})
        if not isinstance(event_counts, dict):
            event_counts = {}
            item["event_counts"] = event_counts
        event_counts[event.event_type] = self._safe_parse_int(event_counts.get(event.event_type), 0) + 1

        recent_events = item.setdefault("recent_events", [])
        if not isinstance(recent_events, list):
            recent_events = []
            item["recent_events"] = recent_events
        recent_events.insert(
            0,
            {
                "type": event.event_type,
                "content": self._single_line_text(event.content, 120),
                "ts": event.ts,
            },
        )
        del recent_events[12:]

        if event.event_type == "danmaku" and event.content:
            recent_danmaku = item.setdefault("recent_danmaku", [])
            if not isinstance(recent_danmaku, list):
                recent_danmaku = []
                item["recent_danmaku"] = recent_danmaku
            text = self._single_line_text(event.content, 80)
            if text:
                recent_danmaku.insert(0, {"content": text, "ts": event.ts})
                seen: set[str] = set()
                deduped: list[dict[str, Any]] = []
                for row in recent_danmaku:
                    if not isinstance(row, dict):
                        continue
                    content = self._single_line_text(row.get("content"), 80)
                    if not content or content in seen:
                        continue
                    seen.add(content)
                    deduped.append({"content": content, "ts": row.get("ts") or time.time()})
                    if len(deduped) >= 8:
                        break
                item["recent_danmaku"] = deduped

        if user_id:
            live_key = f"live:{username}"
            live_item = activity_map.get(live_key)
            if isinstance(live_item, dict) and live_item is not item:
                self._merge_private_companion_viewer_activity(item, live_item)
                activity_map.pop(live_key, None)
            aliases = item.setdefault("live_usernames", [])
            if isinstance(aliases, list) and username not in aliases:
                aliases.insert(0, username)
                del aliases[6:]
        logger.debug(
            "[B站直播] 已更新观众活跃画像: %s type=%s total=%s",
            primary_key,
            event.event_type,
            item.get("total_events"),
        )
        return True

    def _merge_private_companion_viewer_activity(
        self, target: dict[str, Any], source: dict[str, Any]
    ) -> None:
        target["total_events"] = self._safe_parse_int(target.get("total_events"), 0) + self._safe_parse_int(source.get("total_events"), 0)
        target["first_seen"] = min(
            self._safe_parse_float(target.get("first_seen"), time.time()),
            self._safe_parse_float(source.get("first_seen"), time.time()),
        )
        target["last_seen"] = max(
            self._safe_parse_float(target.get("last_seen"), 0),
            self._safe_parse_float(source.get("last_seen"), 0),
        )
        target_counts = target.setdefault("event_counts", {})
        source_counts = source.get("event_counts") if isinstance(source.get("event_counts"), dict) else {}
        if isinstance(target_counts, dict):
            for key, value in source_counts.items():
                target_counts[key] = self._safe_parse_int(target_counts.get(key), 0) + self._safe_parse_int(value, 0)
        for field, limit in (("recent_events", 12), ("recent_danmaku", 8)):
            merged = []
            for row in [*(target.get(field) if isinstance(target.get(field), list) else []), *(source.get(field) if isinstance(source.get(field), list) else [])]:
                if isinstance(row, dict):
                    merged.append(row)
            merged.sort(key=lambda row: self._safe_parse_float(row.get("ts"), 0), reverse=True)
            target[field] = merged[:limit]

    def _record_private_companion_live_memory(
        self,
        plugin: Any,
        event: LiveDanmakuEvent,
        match: dict[str, Any] | None = None,
    ) -> bool:
        if event.event_type not in {
            "danmaku",
            "gift",
            "super_chat",
            "buy_guard",
            "enter_room",
            "follow",
            "like",
        }:
            return False
        username = self._single_line_text(event.username, 40)
        if not username or username in {"系统"}:
            return False
        store = self._private_companion_live_state_store(plugin)
        if not store:
            return False

        now = time.time()
        changed = False
        profile = store.setdefault("stream_profile", {})
        if not isinstance(profile, dict):
            profile = {}
            store["stream_profile"] = profile
        profile["last_event_at"] = now
        profile["total_events"] = self._safe_parse_int(profile.get("total_events"), 0) + 1
        if self._bili_session_started_at:
            profile["current_session_started_at"] = self._bili_session_started_at
        counts = profile.setdefault("event_counts", {})
        if not isinstance(counts, dict):
            counts = {}
            profile["event_counts"] = counts
        counts[event.event_type] = self._safe_parse_int(counts.get(event.event_type), 0) + 1
        changed = True

        if event.event_type == "danmaku" and event.content:
            changed = self._update_live_memory_topics(store, event, username) or changed
            changed = self._maybe_add_live_memory_item(store, event, match) or changed
            changed = self._maybe_add_live_memory_open_thread(store, event, username) or changed
        if event.event_type in self._live_memory_highlight_event_types():
            changed = self._add_live_memory_highlight(store, event, match) or changed
            changed = self._maybe_add_live_memory_item(store, event, match, force=True) or changed
        return changed

    def _update_live_memory_topics(
        self, store: dict[str, Any], event: LiveDanmakuEvent, username: str
    ) -> bool:
        if not self.config.get("live_memory_topic_enabled", True):
            return False
        content = self._single_line_text(event.content, 120)
        if not content:
            return False
        topics = store.setdefault("topic_memory", {})
        if not isinstance(topics, dict):
            topics = {}
            store["topic_memory"] = topics
        candidates = self._extract_live_memory_topics(content)
        if not candidates:
            return False
        now = event.ts or time.time()
        for topic in candidates[:5]:
            item = topics.setdefault(
                topic,
                {"topic": topic, "count": 0, "last_seen": 0, "samples": [], "viewers": []},
            )
            if not isinstance(item, dict):
                item = {"topic": topic, "count": 0, "samples": [], "viewers": []}
                topics[topic] = item
            item["count"] = self._safe_parse_int(item.get("count"), 0) + 1
            item["last_seen"] = now
            viewers = item.setdefault("viewers", [])
            if isinstance(viewers, list) and username not in viewers:
                viewers.insert(0, username)
                del viewers[6:]
            samples = item.setdefault("samples", [])
            if not isinstance(samples, list):
                samples = []
                item["samples"] = samples
            if not any(
                isinstance(row, dict) and row.get("text") == content
                for row in samples
            ):
                samples.insert(0, {"username": username, "text": content, "ts": now})
                del samples[5:]
        self._trim_live_memory_topics(topics)
        return True

    def _extract_live_memory_topics(self, content: str) -> list[str]:
        text = re.sub(r"https?://\S+", "", content)
        raw = re.findall(
            r"#[A-Za-z0-9_\u4e00-\u9fff]{2,24}|[A-Za-z][A-Za-z0-9_]{2,20}|[\u4e00-\u9fff]{2,8}",
            text,
        )
        stopwords = {
            "哈哈",
            "哈哈哈",
            "什么",
            "这个",
            "那个",
            "一下",
            "可以",
            "不是",
            "就是",
            "感觉",
            "真的",
            "今天",
            "直播",
            "主播",
        }
        topics: list[str] = []
        for item in raw:
            topic = item.strip("#").strip()
            if not topic or topic in stopwords:
                continue
            if len(topic) < 2:
                continue
            if topic not in topics:
                topics.append(topic)
        return topics[:8]

    def _trim_live_memory_topics(self, topics: dict[str, Any]) -> None:
        max_topics = max(
            20,
            self._safe_parse_int(self.config.get("live_memory_max_topics"), 80),
        )
        if len(topics) <= max_topics:
            return
        rows = []
        for topic, item in topics.items():
            last_seen = 0.0
            count = 0
            if isinstance(item, dict):
                last_seen = self._safe_parse_float(item.get("last_seen"), 0.0)
                count = self._safe_parse_int(item.get("count"), 0)
            rows.append((count, last_seen, topic))
        rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
        keep = {topic for _count, _last_seen, topic in rows[:max_topics]}
        for topic in list(topics.keys()):
            if topic not in keep:
                topics.pop(topic, None)

    def _maybe_add_live_memory_item(
        self,
        store: dict[str, Any],
        event: LiveDanmakuEvent,
        match: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> bool:
        content = self._single_line_text(event.content, 140)
        if not content:
            return False
        username = self._single_line_text(event.username, 40)
        if event.event_type == "danmaku" and not force:
            pattern = (
                r"喜欢|不喜欢|想看|想听|希望|下次|以后|记得|别忘|"
                r"能不能|可不可以|什么时候|刚才|刚刚|上次|以后还"
            )
            if not re.search(pattern, content):
                return False
        display_name = self._single_line_text((match or {}).get("name"), 40) or username
        text = content
        if event.event_type != "danmaku":
            text = event.display_text()
        item = {
            "id": f"live-memory-{uuid.uuid5(uuid.NAMESPACE_URL, username + '|' + text).hex[:16]}",
            "type": event.event_type,
            "username": username,
            "display_name": display_name,
            "user_id": str((match or {}).get("user_id") or ""),
            "text": self._single_line_text(text, 160),
            "ts": event.ts or time.time(),
            "source": "live_stream_companion",
        }
        items = store.setdefault("memory_items", [])
        if not isinstance(items, list):
            items = []
            store["memory_items"] = items
        if any(isinstance(row, dict) and row.get("id") == item["id"] for row in items):
            return False
        items.insert(0, item)
        max_items = max(
            20,
            self._safe_parse_int(self.config.get("live_memory_max_items"), 80),
        )
        del items[max_items:]
        return True

    def _maybe_add_live_memory_open_thread(
        self, store: dict[str, Any], event: LiveDanmakuEvent, username: str
    ) -> bool:
        content = self._single_line_text(event.content, 120)
        if not content:
            return False
        pattern = r"[?？]|下次|以后|待会|等会|一会|继续|记得|别忘|能不能|可不可以|什么时候|怎么"
        if not re.search(pattern, content):
            return False
        items = store.setdefault("open_threads", [])
        if not isinstance(items, list):
            items = []
            store["open_threads"] = items
        thread_id = uuid.uuid5(uuid.NAMESPACE_URL, username + "|" + content).hex[:16]
        if any(isinstance(row, dict) and row.get("id") == thread_id for row in items):
            return False
        items.insert(
            0,
            {
                "id": thread_id,
                "username": username,
                "text": content,
                "ts": event.ts or time.time(),
                "source": "live_stream_companion",
            },
        )
        max_threads = max(
            6,
            self._safe_parse_int(self.config.get("live_memory_max_open_threads"), 20),
        )
        del items[max_threads:]
        return True

    def _add_live_memory_highlight(
        self,
        store: dict[str, Any],
        event: LiveDanmakuEvent,
        match: dict[str, Any] | None = None,
    ) -> bool:
        text = self._single_line_text(event.display_text(), 180)
        if not text:
            return False
        items = store.setdefault("highlight_events", [])
        if not isinstance(items, list):
            items = []
            store["highlight_events"] = items
        highlight_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            event.event_type + "|" + event.username + "|" + text,
        ).hex[:16]
        if any(isinstance(row, dict) and row.get("id") == highlight_id for row in items):
            return False
        items.insert(
            0,
            {
                "id": highlight_id,
                "type": event.event_type,
                "username": self._single_line_text(event.username, 40),
                "display_name": self._single_line_text((match or {}).get("name"), 40),
                "text": text,
                "ts": event.ts or time.time(),
                "source": "live_stream_companion",
            },
        )
        max_highlights = max(
            10,
            self._safe_parse_int(self.config.get("live_memory_max_highlights"), 40),
        )
        del items[max_highlights:]
        return True

    def _write_private_companion_viewer_memory(
        self,
        plugin: Any,
        match: dict[str, Any],
        event: LiveDanmakuEvent,
        event_key: str,
    ) -> bool:
        if event.event_type not in self._private_companion_writeback_event_types():
            return False
        data = getattr(plugin, "data", None)
        profiles = data.get("worldbook_member_profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, dict):
            return False
        user_id = str(match.get("user_id") or "").strip()
        profile = profiles.get(user_id)
        if not isinstance(profile, dict):
            return False
        memories = profile.setdefault("important_memories", [])
        if not isinstance(memories, list):
            memories = []
            profile["important_memories"] = memories
        source_id = f"live_stream_companion:{event_key}"
        if any(isinstance(item, dict) and item.get("source_id") == source_id for item in memories):
            return False
        title = {
            "gift": "直播间送礼",
            "super_chat": "直播间醒目留言",
            "buy_guard": "直播间上舰",
        }.get(event.event_type, "直播间互动")
        content = (
            f"直播用户名 {event.username} 在 B站直播间"
            f"{event.content if event.event_type != 'danmaku' else '发弹幕：' + event.content}"
        )
        memories.insert(
            0,
            {
                "title": title,
                "content": self._single_line_text(content, 220),
                "weight": 75 if event.event_type in {"super_chat", "buy_guard"} else 62,
                "privacy": "internal",
                "source": "live_stream_companion",
                "source_id": source_id,
                "enabled": True,
                "updated_at": time.time(),
            },
        )
        profile["important_memories"] = [
            item for item in memories if isinstance(item, dict)
        ][:8]
        profile["manual_edit_ts"] = time.time()
        logger.info(
            "[B站直播] 已写入陪伴关系记忆: user=%s event=%s",
            user_id,
            event.event_type,
        )
        return True

    def _maybe_register_private_companion_live_viewer(
        self, plugin: Any, event: LiveDanmakuEvent
    ) -> bool:
        if event.event_type not in {"danmaku", "gift", "super_chat", "buy_guard"}:
            return False
        username = self._single_line_text(event.username, 40)
        if not username or username in {"观众", "系统"}:
            return False
        store = self._private_companion_live_state_store(plugin)
        observations = store.setdefault("viewer_observations", {})
        if not isinstance(observations, dict):
            observations = {}
            store["viewer_observations"] = observations
        item = observations.setdefault(
            username,
            {
                "username": username,
                "count": 0,
                "first_seen": time.time(),
                "recent_events": [],
                "profile_id": "",
            },
        )
        if not isinstance(item, dict):
            item = {"username": username, "count": 0, "recent_events": []}
            observations[username] = item
        item["count"] = self._safe_parse_int(item.get("count"), 0) + 1
        item["last_seen"] = time.time()
        recent = item.setdefault("recent_events", [])
        if not isinstance(recent, list):
            recent = []
            item["recent_events"] = recent
        recent.insert(
            0,
            {
                "type": event.event_type,
                "content": self._single_line_text(event.content, 120),
                "ts": event.ts,
            },
        )
        del recent[8:]

        min_events = max(
            1,
            self._safe_parse_int(
                self.config.get("private_companion_auto_register_min_events"),
                2,
            ),
        )
        if item.get("profile_id") or item["count"] < min_events:
            return True

        bili_uid = self._bili_event_uid(event.raw)
        external_ids = self._private_companion_live_external_ids(username, event)
        profile_id = f"bili:{bili_uid}" if bili_uid else "bili_live_" + uuid.uuid5(uuid.NAMESPACE_URL, username).hex[:16]
        data = getattr(plugin, "data", None)
        profiles = data.setdefault("worldbook_member_profiles", {}) if isinstance(data, dict) else {}
        if not isinstance(profiles, dict):
            return True
        if profile_id not in profiles:
            profiles[profile_id] = {
                "user_id": profile_id,
                "identity_type": "external",
                "name": username,
                "aliases": [],
                "observed_names": [username],
                "external_ids": sorted(external_ids),
                "content": f"B站直播间观众，直播用户名 {username}。身份尚未与 QQ 号确认。",
                "identity_note": f"B站直播间观众，直播用户名 {username}；可能需要后续人工合并到真实关系节点。",
                "boundary_note": "直播身份为候选登记，不要在公开场景提及内部匹配或关系网。",
                "important_memories": [],
                "pending_observations": [
                    {
                        "id": f"live-{int(time.time())}",
                        "title": "直播观众自动登记",
                        "content": self._single_line_text(
                            f"{username} 在直播间出现 {item['count']} 次，最近互动：{event.content}",
                            240,
                        ),
                        "evidence": self._single_line_text(event.display_text(), 240),
                        "weight": 35,
                        "source": "live_stream_companion",
                        "created_at": time.time(),
                    }
                ],
                "enabled": True,
                "priority": 80,
                "auto_registration_pending": True,
                "source": "live_stream_companion",
                "manual_edit_ts": time.time(),
            }
            logger.info("[B站直播] 已自动登记直播观众候选关系: %s", username)
        else:
            profile = profiles.get(profile_id)
            if isinstance(profile, dict):
                profile["identity_type"] = "external"
                ids = profile.setdefault("external_ids", [])
                if not isinstance(ids, list):
                    ids = []
                    profile["external_ids"] = ids
                for ext in sorted(external_ids):
                    if ext not in ids:
                        ids.append(ext)
                if username not in (profile.get("observed_names") if isinstance(profile.get("observed_names"), list) else []):
                    observed = profile.setdefault("observed_names", [])
                    if isinstance(observed, list):
                        observed.insert(0, username)
                        del observed[8:]
        item["profile_id"] = profile_id
        return True

    def _maybe_apply_private_companion_live_state(
        self, plugin: Any, event: LiveDanmakuEvent
    ) -> bool:
        cooldown = max(
            30.0,
            self._safe_parse_float(
                self.config.get("private_companion_live_state_cooldown_seconds"),
                300.0,
            ),
        )
        now = time.time()
        if now - self._private_companion_last_state_at < cooldown:
            return False
        session_events = list(self._bili_session_events)
        recent_events = [item for item in session_events if now - item.ts <= 300]
        significant = [
            item
            for item in recent_events
            if item.event_type in {"gift", "super_chat", "buy_guard"}
        ]
        if event.event_type not in {"gift", "super_chat", "buy_guard"} and len(recent_events) < 5:
            return False
        label = "直播间互动很热闹，状态被观众带得更轻快"
        mood = "轻快"
        energy_delta = 4
        intensity = 58
        if significant:
            label = "直播间收到礼物或醒目留言，情绪被明显点亮"
            energy_delta = 6
            intensity = 68
        make_condition = getattr(plugin, "_make_condition", None)
        compose = getattr(plugin, "_compose_state_from_conditions", None)
        if not callable(make_condition) or not callable(compose):
            return False
        data = getattr(plugin, "data", None)
        if not isinstance(data, dict):
            return False
        conditions = data.setdefault("state_conditions", [])
        if not isinstance(conditions, list):
            conditions = []
            data["state_conditions"] = conditions
        conditions.append(
            make_condition(
                kind="live_stream",
                title="直播间互动",
                label=label,
                mood=mood,
                energy_delta=energy_delta,
                duration_hours=max(
                    1,
                    self._safe_parse_int(
                        self.config.get("private_companion_live_state_duration_hours"),
                        2,
                    ),
                ),
                intensity=intensity,
                cause=f"B站直播间最近 {len(recent_events)} 条互动",
                phase="live_afterglow",
                episode_key=self._private_companion_today_key("live-stream"),
            )
        )
        weather = data.get("daily_weather") if isinstance(data.get("daily_weather"), dict) else {}
        data["daily_state"] = compose(weather)
        self._private_companion_last_state_at = now
        return True

    async def _write_private_companion_live_summary(self) -> None:
        if not (
            self._private_companion_writeback_enabled()
            or self._live_memory_enabled()
        ):
            return
        if not self.config.get("private_companion_live_summary_enabled", True):
            return
        events = list(self._bili_session_events)
        if not events:
            return
        if self._bili_summary_written_for_session:
            return
        self._bili_summary_written_for_session = True
        plugin = self._get_private_companion_plugin()
        if plugin is None:
            self._bili_summary_written_for_session = False
            return
        changed = False
        try:
            lock = getattr(plugin, "_data_lock", None)
            if lock is not None:
                async with lock:
                    changed = self._write_private_companion_live_summary_locked(plugin, events)
                    if changed:
                        self._save_private_companion(plugin)
            else:
                changed = self._write_private_companion_live_summary_locked(plugin, events)
                if changed:
                    self._save_private_companion(plugin)
        except Exception as e:
            logger.debug(f"[B站直播] 写入陪伴插件直播小结失败: {e}")
            self._bili_summary_written_for_session = False
            return
        if changed:
            self._bili_session_events.clear()
            self._bili_session_started_at = 0.0

    def _write_private_companion_live_summary_locked(
        self, plugin: Any, events: list[LiveDanmakuEvent]
    ) -> bool:
        data = getattr(plugin, "data", None)
        if not isinstance(data, dict):
            return False
        summary = self._build_live_summary_payload(events)
        if not summary:
            return False
        store = self._private_companion_live_state_store(plugin)
        summaries = store.setdefault("summaries", [])
        if not isinstance(summaries, list):
            summaries = []
            store["summaries"] = summaries
        summaries.append(summary)
        del summaries[:-20]

        if not self._private_companion_writeback_enabled():
            logger.info("[B站直播] 已写入直播专用记忆小结: %s", summary["summary"])
            return True

        diaries = data.setdefault("bot_diaries", [])
        if not isinstance(diaries, list):
            diaries = []
            data["bot_diaries"] = diaries
        diary = {
            "date": summary["date"],
            "kind": "live_stream_summary",
            "summary": summary["summary"],
            "body": summary["body"],
            "share_seed": summary["share_seed"],
            "tags": ["直播", "互动", *summary["tags"]],
            "today_events": summary["today_events"],
            "proactive_events": [],
            "dream_fragments": [
                {
                    "text": self._single_line_text(summary["share_seed"], 120),
                    "weight": 0.5,
                    "source": "live_stream_companion",
                }
            ],
            "long_term_events": [],
            "generated_at": time.time(),
            "source": "live_stream_companion",
        }
        diaries.append(diary)
        max_diaries = self._safe_parse_int(getattr(plugin, "max_diary_entries", 30), 30)
        del diaries[:-max(1, max_diaries)]
        logger.info("[B站直播] 已写入陪伴插件直播小结: %s", summary["summary"])
        return True

    def _build_live_summary_payload(
        self, events: list[LiveDanmakuEvent]
    ) -> dict[str, Any]:
        if not events:
            return {}
        counts: dict[str, int] = {}
        viewers: dict[str, int] = {}
        highlights: list[str] = []
        for event in events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
            if event.username and event.username != "系统":
                viewers[event.username] = viewers.get(event.username, 0) + 1
            if event.event_type in {"gift", "super_chat", "buy_guard"}:
                highlights.append(event.display_text())
        top_viewers = sorted(viewers.items(), key=lambda item: item[1], reverse=True)[:5]
        count_text = "、".join(f"{key} {value} 条" for key, value in counts.items())
        viewer_text = "、".join(f"{name}({count})" for name, count in top_viewers) or "零散观众"
        highlight_text = "；".join(self._single_line_text(item, 80) for item in highlights[:5])
        started = self._bili_session_started_at or events[0].ts
        ended = max(item.ts for item in events)
        duration_minutes = max(1, int((ended - started) / 60))
        summary = (
            f"本次直播约 {duration_minutes} 分钟，收到 {len(events)} 条互动"
            f"（{count_text or '暂无分类'}）。"
        )
        body = (
            f"今晚直播间留下一段挺具体的互动：{summary}"
            f"常出现的观众有 {viewer_text}。"
        )
        if highlight_text:
            body += f" 其中比较亮的片段是：{highlight_text}。"
        else:
            body += " 大多是普通弹幕和轻轻接话，气氛更像有人在旁边陪着说几句。"
        share_seed = (
            f"直播间刚刚有 {len(events)} 条互动，"
            f"{'还有礼物或醒目留言' if highlights else '主要是弹幕聊天'}。"
        )
        return {
            "id": f"live-summary-{int(time.time())}",
            "date": time.strftime("%Y-%m-%d", time.localtime(ended)),
            "started_at": started,
            "ended_at": ended,
            "duration_minutes": duration_minutes,
            "counts": counts,
            "top_viewers": [{"name": name, "count": count} for name, count in top_viewers],
            "highlights": highlights[:8],
            "summary": summary,
            "body": body,
            "share_seed": share_seed,
            "tags": ["礼物"] if highlights else ["弹幕"],
            "today_events": [
                {
                    "window": time.strftime("%H:%M", time.localtime(started))
                    + "-"
                    + time.strftime("%H:%M", time.localtime(ended)),
                    "event": summary,
                    "mood": "轻快" if highlights else "平稳",
                }
            ],
        }

    def _private_companion_today_key(self, suffix: str = "") -> str:
        today = time.strftime("%Y-%m-%d", time.localtime())
        return f"{suffix}-{today}" if suffix else today

    def _save_private_companion(self, plugin: Any) -> None:
        saver = getattr(plugin, "_save_data_sync", None)
        if callable(saver):
            saver()

    def _build_bili_support_reply_hint(self, events: list[LiveDanmakuEvent]) -> str:
        if not any(event.event_type in {"gift", "super_chat"} for event in events):
            return ""
        return (
            "\n\n本批直播事件包含礼物或醒目留言。请优先感谢送礼物/SC 的观众，"
            "自然提到观众名和礼物或 SC 内容；不要机械复读数量，不要像播报清单。"
        )

    async def _inject_bili_live_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self.config.get("bili_live_inject_enabled", True):
            return
        if not self._is_bili_live_enabled():
            return
        if not self._is_bili_live_running():
            return

        include_events = self.config.get("bili_live_inject_event_types", ["danmaku"])
        if not isinstance(include_events, list):
            include_events = ["danmaku"]
        events = self._recent_bili_events(include_events=include_events)
        formatted = self._format_bili_events(events)
        if not formatted:
            return

        prompt = (
            "## B站直播间实时信息\n"
            "以下是你当前可以读取到的最近 B站直播间事件。它们是实时上下文，不一定需要逐条回应；"
            "当用户要求你看弹幕、回应直播间观众，或当前对话和直播互动相关时，可以自然引用。\n"
            "不要伪造未列出的弹幕、礼物或观众行为。\n"
            f"{formatted}"
        )
        auxiliary_context = self._build_bili_live_auxiliary_context(events)
        if auxiliary_context:
            prompt += "\n\n" + auxiliary_context
        req.system_prompt += "\n\n" + prompt + "\n"

    @filter.command("bili_live_start")
    async def cmd_bili_live_start(self, event: AstrMessageEvent, room_id: int = 0):
        """启动 B站直播弹幕监听，可传入房间号，否则使用配置项。"""
        if not self._is_bili_live_enabled():
            yield event.plain_result(
                "B站直播功能未启用，请先在插件配置中开启 bilibili_enabled。"
            )
            return

        bili_type = self._get_bili_live_type()
        target_room_id = room_id or self._get_config_room_id()
        if bili_type == "web" and not target_room_id:
            yield event.plain_result(
                "请提供 B站直播房间号，例如 /bili_live_start 123456，或在插件配置中填写。"
            )
            return
        message = await self._start_bili_live(target_room_id)
        yield event.plain_result(message)

    @filter.command("bili_live_stop")
    async def cmd_bili_live_stop(self, event: AstrMessageEvent):
        """停止 B站直播弹幕监听。"""
        message = await self._stop_bili_live()
        yield event.plain_result(message)

    @filter.command("bili_live_status")
    async def cmd_bili_live_status(self, event: AstrMessageEvent):
        """查看 B站直播弹幕监听状态。"""
        enabled = self._is_bili_live_enabled()
        status = "运行中" if self._is_bili_live_running() else "未运行"
        room_id = (
            self._bili_live_client.real_room_id
            if self._bili_live_client and self._bili_live_client.real_room_id
            else self._get_config_room_id()
        )
        latest = self._bili_events[-1].display_text() if self._bili_events else "暂无"
        backend_text = (
            f"{self._get_bili_live_type()}/{self._get_bili_web_backend()}"
            if self._get_bili_live_type() == "web"
            else self._get_bili_live_type()
        )
        last_error = (
            getattr(self._bili_live_client, "last_error", "")
            or self._get_bili_live_task_error()
            or "无"
        )
        yield event.plain_result(
            f"B站直播功能：{'已启用' if enabled else '未启用'}\n"
            f"B站直播弹幕监听：{status}\n"
            f"监听后端：{backend_text}\n"
            f"房间号：{room_id or '未配置'}\n"
            f"已缓存事件：{len(self._bili_events)} 条\n"
            f"最近事件：{latest}\n"
            f"最近错误：{last_error}"
        )

    @filter.command("分区")
    @filter.command("bili_live_area")
    async def cmd_bili_live_area(self, event: AstrMessageEvent, query: str = ""):
        """按分区名、拼音或 area_id 设置 B站直播分区。"""
        query = str(query or "").strip()
        if query.lower() in {"refresh", "reload"} or query in {"刷新", "重载"}:
            loaded = await self._ensure_bili_area_cache(force=True)
            yield event.plain_result(
                f"直播分区列表已刷新，共 {len(self._bili_area_by_id)} 个子分区。"
                if loaded
                else "直播分区列表刷新失败，请稍后再试。"
            )
            return

        if not query:
            area = await self._find_bili_area(self.config.get("area_id"))
            current = area.display_text() if area else (
                f"part_id={self.config.get('part_id') or '未配置'}, "
                f"area_id={self.config.get('area_id') or '未配置'}"
            )
            yield event.plain_result(
                "当前 B站直播分区："
                f"{current}\n"
                "用法：/分区 英雄联盟、/分区 yingxionglianmeng、/分区 86"
            )
            return

        area = await self._find_bili_area(query)
        if not area:
            yield event.plain_result(
                "没有找到这个 B站直播分区。可以输入子分区名、拼音或 area_id，"
                "例如 /分区 英雄联盟、/分区 yingxionglianmeng、/分区 86。"
            )
            return

        persisted = await self._persist_plugin_config_updates(
            {"part_id": area.part_id, "area_id": area.area_id}
        )
        warning = "\n提示：该分区可能受限，B站侧可能不允许随便设置。" if area.locked else ""
        persisted_text = "已写入配置。" if persisted else "已应用到当前运行实例，但未确认持久化。"
        yield event.plain_result(
            f"已设置 B站直播分区：{area.display_text()}\n"
            f"{persisted_text}{warning}"
        )

    @filter.command("bili_live_debug")
    async def cmd_bili_live_debug(self, event: AstrMessageEvent, enabled: bool = True):
        """开启/关闭 B站直播调试日志。"""
        self._bili_debug_mode = bool(enabled)
        if isinstance(
            self._bili_live_client,
            (BilibiliLiveClient, BilibiliBlivedmClient, BilibiliLaplaceClient),
        ):
            self._bili_live_client.debug_log = self._bili_debug_mode
        yield event.plain_result(
            f"B站直播调试日志已{'开启' if self._bili_debug_mode else '关闭'}。"
            "如果需要看到 debug 级别日志，请同时确认 AstrBot 日志级别允许 debug 输出。"
        )

    @filter.command("bili_live_bind_here")
    async def cmd_bili_live_bind_here(self, event: AstrMessageEvent):
        """将当前聊天绑定为 B站直播自动回应输出会话。"""
        await self.put_kv_data(KV_KEY_BILI_REPLY_SESSION, event.unified_msg_origin)
        self._bili_reply_event_template = copy.copy(event)
        self._bili_reply_event_template.message_obj = copy.copy(event.message_obj)
        yield event.plain_result(
            "已将当前聊天绑定为 B站直播自动回应会话。开启 bili_live_auto_reply_enabled 后，"
            "直播弹幕会以 AstrBot 原生消息事件的方式触发 Bot 在这里回复。"
        )

    @filter.command("bili_live_probe")
    async def cmd_bili_live_probe(self, event: AstrMessageEvent, room_id: int = 0):
        """诊断 B站直播间信息和弹幕服务器信息。"""
        target_room_id = room_id or self._get_config_room_id()
        if not target_room_id:
            yield event.plain_result("请提供房间号，例如 /bili_live_probe 123456。")
            return
        try:
            info = await probe_bilibili_live_room(
                target_room_id,
                sessdata=self._get_bili_sessdata(),
            )
            lines = [
                "B站直播间诊断结果：",
                f"输入房间号：{target_room_id}",
                f"真实房间号：{info.get('real_room_id')}",
                f"直播状态：{info.get('live_status')}（0未开播，1直播中，2轮播）",
                f"房间接口：code={info.get('room_init_code')} message={info.get('room_init_message')}",
                f"弹幕接口：code={info.get('danmu_info_code')} message={info.get('danmu_info_message')}",
                f"弹幕风控：{'是' if info.get('danmu_risk_control') else '否'}",
                f"弹幕 token：{'有' if info.get('danmu_token_present') else '无'}",
                f"弹幕服务器数：{info.get('danmu_host_count')}",
                f"服务器示例：{', '.join(info.get('danmu_hosts') or []) or '无'}",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.warning(f"[B站直播] 直播间诊断失败: {e}")
            yield event.plain_result(f"B站直播间诊断失败：{e}")

    @filter.command("bili_live_recent")
    async def cmd_bili_live_recent(self, event: AstrMessageEvent, limit: int = 10):
        """查看最近缓存的 B站直播弹幕/事件。"""
        if not self._is_bili_live_enabled():
            yield event.plain_result(
                "B站直播功能未启用，请先在插件配置中开启 bilibili_enabled。"
            )
            return

        events = self._recent_bili_events(limit=limit, include_events=[])
        formatted = self._format_bili_events(events)
        if not formatted:
            formatted = await self._format_bili_history_fallback(limit)
        yield event.plain_result(formatted or "暂时还没有读取到 B站直播事件。")

    @filter.command("bili_live_memory")
    async def cmd_bili_live_memory(self, event: AstrMessageEvent, limit: int = 8):
        """查看直播专用记忆上下文。"""
        if not self._live_memory_enabled():
            yield event.plain_result("直播专用记忆未启用，请开启 live_memory_enabled。")
            return
        plugin = self._get_private_companion_plugin()
        if plugin is None:
            yield event.plain_result(
                "暂时无法读取直播专用记忆：未找到正在运行的“我会永远陪着你”插件实例。"
            )
            return
        overview = self._format_live_memory_overview(plugin, limit=limit)
        if not overview:
            yield event.plain_result("暂时还没有可用的直播专用记忆。")
            return
        yield event.plain_result("直播专用记忆：\n" + overview)

    @llm_tool(name="bili_live_recent_danmaku")
    async def tool_bili_live_recent_danmaku(
        self, event: AstrMessageEvent, limit: int = 8
    ):
        """
        读取最近的 B站直播弹幕和直播间事件。适合在用户询问直播弹幕、要求回应观众、
        或需要了解直播间实时互动时调用。

        Args:
            limit(number): 返回最近多少条事件，默认 8，最大 30。
        """
        if not self._is_bili_live_enabled():
            return "B站直播功能未启用，请先在插件配置中开启 bilibili_enabled。"

        limit = min(30, max(1, int(limit or 8)))
        events = self._recent_bili_events(limit=limit, include_events=[])
        formatted = self._format_bili_events(events)
        if not formatted:
            formatted = await self._format_bili_history_fallback(limit)
        if not formatted:
            if self._is_bili_live_running():
                return "B站直播弹幕监听正在运行，但暂时还没有读取到事件。"
            return "B站直播弹幕监听未运行，请先使用 /bili_live_start <房间号> 启动。"
        return "最近的 B站直播间事件：\n" + formatted

    @llm_tool(name="bili_live_memory_context")
    async def tool_bili_live_memory_context(
        self, event: AstrMessageEvent, limit: int = 8
    ):
        """
        读取直播专用记忆上下文。适合在用户询问直播间老梗、常聊话题、
        观众偏好、直播高光或下播小结时调用。

        Args:
            limit(number): 每类最多返回多少条，默认 8，最大 30。
        """
        if not self._live_memory_enabled():
            return "直播专用记忆未启用，请开启 live_memory_enabled。"
        plugin = self._get_private_companion_plugin()
        if plugin is None:
            return "未找到正在运行的“我会永远陪着你”插件实例，暂时无法读取直播专用记忆。"
        overview = self._format_live_memory_overview(plugin, limit=limit)
        if not overview:
            return "暂时还没有可用的直播专用记忆。"
        return "直播专用记忆：\n" + overview

    async def _format_bili_history_fallback(self, limit: int = 10) -> str:
        client = self._bili_live_client
        fetcher = getattr(client, "fetch_recent_history_events", None)
        if not fetcher:
            return ""
        try:
            events = await fetcher(limit)
        except Exception as e:
            logger.debug(f"[B站直播] 读取历史弹幕兜底失败: {e}")
            return ""
        return self._format_bili_events(events)

    # ------------------------------------------------------------------ #
    #  自主 Live2D 标签机制
    # ------------------------------------------------------------------ #

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在模型回复前注入直播弹幕上下文和可选 Live2D 标签说明。"""
        await self._inject_bili_live_context(event, req)

        if not self.config.get("autonomous_l2d_enabled", True):
            return

        entries = self._get_l2d_entries()
        if not entries:
            return
        if not await self._check_and_reconnect():
            logger.debug("[VTS] 未连接 Live2D，跳过 L2D 标签提示词注入")
            return

        max_tags = int(self.config.get("l2d_max_tags_per_reply", 1) or 1)
        max_tags = max(1, max_tags)
        lines = [
            "## Live2D 表情控制",
            "你可以通过在回复末尾输出 Live2D 标签来控制当前 Live2D 模型表情。",
            "标签只用于控制表情，不是给用户看的内容。正常回答用户，然后在最后单独输出一行标签。",
            f"格式：<l2d:标签名>。最多选择 {max_tags} 个；多个标签可写成 <l2d:标签1,标签2>。",
            "如果本次回复不适合使用表情，输出 <l2d:none>。",
            "不要解释标签，不要编造未列出的标签。",
            "",
            "可选表情按键：",
        ]
        for entry in entries:
            desc = entry["description"] or "无额外说明"
            duration = entry["duration"]
            duration_text = f"{duration:g} 秒" if duration > 0 else "不自动结束"
            lines.append(
                f"- {entry['tag']}（{entry['name']}）: {desc}；持续时间：{duration_text}；热键ID：{entry['hotkey_id']}"
            )

        req.system_prompt += "\n\n" + "\n".join(lines) + "\n"

    @filter.on_llm_response(priority=2000)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """处理模型回复中的 Live2D 标签。字幕会在最终消息链阶段推送。"""
        completion_text = getattr(resp, "completion_text", None)
        if not isinstance(completion_text, str) or not completion_text.strip():
            return

        if self.config.get("autonomous_l2d_enabled", True) and "<l2d" in completion_text.lower():
            tags, cleaned = self._parse_l2d_tags(completion_text)
            if cleaned != completion_text:
                resp.completion_text = cleaned

            tags = [tag for tag in tags if tag.lower() not in {"none", "无", "null", "no"}]
            if tags:
                max_tags = int(self.config.get("l2d_max_tags_per_reply", 1) or 1)
                self._create_l2d_task(self._trigger_l2d_tags(tags[: max(1, max_tags)]))

    @filter.on_decorating_result(priority=100000000000000000)
    async def on_subtitle_decorating_result(self, event: AstrMessageEvent):
        """在 TTS 语音生成完成后，同步启动字幕和嘴型联动。"""
        if not self._is_subtitle_enabled() and not self._is_mouth_sync_enabled():
            return
        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return

        if not getattr(result, "__vts_mouth_sync_processed", False):
            setattr(result, "__vts_mouth_sync_processed", True)
            await self._start_mouth_sync_for_result(result)

        if not self._is_subtitle_enabled():
            return
        if bool(event.get_extra("bili_live_skip_subtitle")):
            return
        if not self._event_should_push_subtitle(event):
            return
        if getattr(result, "__vts_subtitle_processed", False):
            return

        setattr(result, "__vts_subtitle_processed", True)

        text = self._extract_subtitle_text_from_result(result)
        await self._push_subtitle(text, source="bili_live" if event.get_extra("bili_live_auto_reply") else "")

    @filter.command("vts_l2d_list")
    async def cmd_vts_l2d_list(self, event: AstrMessageEvent):
        """列出自主 Live2D 标签配置。"""
        entries = self._get_l2d_entries()
        if not entries:
            yield event.plain_result("当前没有启用的 L2D 标签条目，请先在插件配置中添加。")
            return

        lines = ["当前启用的 L2D 标签："]
        for entry in entries:
            duration = entry["duration"]
            duration_text = f"{duration:g} 秒" if duration > 0 else "不自动结束"
            lines.append(
                f"• {entry['name']}：<l2d:{entry['tag']}> -> {entry['hotkey_id']} | {duration_text} | "
                f"{entry['description'] or '无说明'}"
            )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    #  Token 持久化（使用框架 KV 存储）
    # ------------------------------------------------------------------ #

    async def _load_token(self) -> Optional[str]:
        """从框架 KV 存储加载 Token"""
        return await self.get_kv_data(KV_KEY_TOKEN, None)

    async def _save_token(self, token: str):
        """保存 Token 到框架 KV 存储"""
        await self.put_kv_data(KV_KEY_TOKEN, token)

    async def _ensure_connection(self) -> str:
        """确保连接可用，返回错误消息或空字符串"""
        if not await self._check_and_reconnect():
            return "❌ 未连接到 VTube Studio，请先发送 /vts_auth 进行认证。"
        return ""

    # ------------------------------------------------------------------ #
    #  命令
    # ------------------------------------------------------------------ #

    @filter.command("vts_auth")
    async def cmd_vts_auth(self, event: AstrMessageEvent):
        """发送 /vts_auth 触发 VTube Studio 认证流程"""
        yield event.plain_result(
            "正在向 VTube Studio 申请认证 Token，请在 VTS 界面点击【允许】按钮..."
        )
        try:
            token = await self.vts.request_auth_token()
            ok = await self.vts.authenticate(token)
            if ok:
                await self._save_token(token)
                self._connected = True
                yield event.plain_result(
                    "✅ VTube Studio 认证成功！Token 已保存。\n"
                    "现在 LLM 可以控制你的 Live2D 模型了。"
                )
            else:
                yield event.plain_result("❌ 认证失败，请确认已在 VTS 界面点击允许。")
        except VTSConnectionError as e:
            yield event.plain_result(f"❌ 连接失败：{e}")
        except VTSTimeoutError as e:
            yield event.plain_result(f"❌ 连接超时：{e}")
        except Exception as e:
            yield event.plain_result(
                f"❌ 认证出错：{e}\n"
                "请确保 VTube Studio 已启动并开启了 API。\n"
                "可先发送 /vts_discover 重新扫描。"
            )

    @filter.command("vts_discover")
    async def cmd_vts_discover(self, event: AstrMessageEvent):
        """重新扫描并自动发现 VTube Studio 的运行地址"""
        yield event.plain_result(f"🔍 正在扫描 VTube Studio（{platform.system()} 平台）...")
        try:
            info = get_install_info()
            host, port = await auto_discover()

            self.vts.url = f"ws://{host}:{port}"
            await self.vts.reset_connection()

            lines = [
                f"🖥️ 操作系统：{info['os']}",
                f"📂 安装路径：{info['install_path'] or '未找到'}",
                f"⚙️ 配置文件端口：{info['config_port'] or '未读取到'}",
                f"🔄 进程运行中：{'是' if info['process_running'] else '否（需要 psutil）'}",
                "",
                f"✅ 已将连接地址更新为 ws://{host}:{port}",
                "",
                "如需认证请发送 /vts_auth",
            ]
            yield event.plain_result("\n".join(lines))

            saved_token = await self._load_token()
            if saved_token:
                ok = await self.vts.authenticate(saved_token)
                if ok:
                    self._connected = True
                    yield event.plain_result("🔗 已用保存的 Token 重新认证成功！")
        except Exception as e:
            yield event.plain_result(f"❌ 自动发现失败：{e}")

    @filter.command("vts_status")
    async def cmd_vts_status(self, event: AstrMessageEvent):
        """查询 VTube Studio 连接状态和当前模型信息"""
        if not await self._check_and_reconnect():
            yield event.plain_result(
                "❌ 未连接到 VTube Studio。\n"
                "• 发送 /vts_discover 自动扫描\n"
                "• 发送 /vts_auth 进行认证"
            )
            return
        try:
            model_info = await self.vts.get_model_info()
            hotkeys = await self.vts.get_hotkeys()
            expressions = await self.vts.get_expressions()

            hotkey_names = [h.get("name", h.get("hotkeyID", "?")) for h in hotkeys]
            expr_names = [e.get("file", "?") for e in expressions]

            msg = (
                f"✅ VTube Studio 已连接（{self.vts.url}）\n"
                f"🖥️ 平台：{platform.system()}\n"
                f"📦 当前模型：{model_info.get('modelName', '未知')}\n"
                f"🎬 可用热键（{len(hotkeys)} 个）：{', '.join(hotkey_names[:10]) or '无'}\n"
                f"😊 可用表情（{len(expressions)} 个）：{', '.join(expr_names[:10]) or '无'}"
            )
            yield event.plain_result(msg)
        except VTSConnectionError as e:
            self._connected = False
            yield event.plain_result(f"❌ 连接已断开：{e}")
        except Exception as e:
            yield event.plain_result(f"❌ 查询失败：{e}")

    @filter.command("vts_list")
    async def cmd_vts_list(self, event: AstrMessageEvent):
        """列出所有热键和表情"""
        if not await self._check_and_reconnect():
            yield event.plain_result("❌ 未连接到 VTube Studio，请先发送 /vts_auth 进行认证。")
            return
        try:
            hotkeys = await self.vts.get_hotkeys()
            expressions = await self.vts.get_expressions()

            lines = ["🎬 **热键列表**"]
            for h in hotkeys:
                lines.append(
                    f"  • {h.get('name', '?')}  "
                    f"(ID: {h.get('hotkeyID', '?')}，类型: {h.get('type', '?')})"
                )
            lines.append("\n😊 **表情列表**")
            for e in expressions:
                active_mark = "✅" if e.get("active") else "⬜"
                lines.append(f"  {active_mark} {e.get('file', '?')}")

            yield event.plain_result("\n".join(lines))
        except VTSConnectionError as e:
            self._connected = False
            yield event.plain_result(f"❌ 连接已断开：{e}")
        except Exception as e:
            yield event.plain_result(f"❌ 查询失败：{e}")

    # ------------------------------------------------------------------ #
    #  LLM 工具函数
    # ------------------------------------------------------------------ #

    @llm_tool(name="vts_trigger_hotkey")
    async def tool_trigger_hotkey(self, event: AstrMessageEvent, hotkey_id: str):
        """
        触发 VTube Studio 中的热键，可以播放动作动画、切换表情、改变待机动画等。
        使用前建议先用 vts_get_hotkeys 获取可用热键列表。

        Args:
            hotkey_id(string): 热键的名称或唯一ID，例如 "wave" 或 "Smile"
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            result = await self.vts.trigger_hotkey(hotkey_id)
            return f"✅ 已触发热键「{hotkey_id}」。结果：{json.dumps(result, ensure_ascii=False)}"
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except VTSTimeoutError as e:
            return f"❌ 请求超时：{e}"
        except Exception as e:
            return f"❌ 触发热键失败：{e}"

    @llm_tool(name="vts_get_hotkeys")
    async def tool_get_hotkeys(self, event: AstrMessageEvent):
        """
        获取 VTube Studio 当前模型可用的所有热键列表（包括动作、表情热键等）。
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            hotkeys = await self.vts.get_hotkeys()
            if not hotkeys:
                return "当前模型没有可用热键。"
            lines = ["当前模型可用热键："]
            for h in hotkeys:
                lines.append(
                    f"• 名称: {h.get('name','?')}, "
                    f"ID: {h.get('hotkeyID','?')}, "
                    f"类型: {h.get('type','?')}"
                )
            return "\n".join(lines)
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except Exception as e:
            return f"❌ 获取热键列表失败：{e}"

    @llm_tool(name="vts_set_expression")
    async def tool_set_expression(
        self,
        event: AstrMessageEvent,
        expression_file: str,
        active: bool = True,
        fade_time: float = 0.25,
    ):
        """
        激活或停用 VTube Studio 中的指定表情。
        使用前建议先用 vts_get_expressions 获取可用表情列表。

        Args:
            expression_file(string): 表情文件名，例如 "happy.exp3.json"
            active(boolean): true 表示激活表情，false 表示停用表情，默认 true
            fade_time(number): 淡入淡出时间（秒），默认 0.25
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            result = await self.vts.set_expression(expression_file, active, fade_time)
            action = "激活" if active else "停用"
            return f"✅ 已{action}表情「{expression_file}」。结果：{json.dumps(result, ensure_ascii=False)}"
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except VTSTimeoutError as e:
            return f"❌ 请求超时：{e}"
        except Exception as e:
            return f"❌ 设置表情失败：{e}"

    @llm_tool(name="vts_get_expressions")
    async def tool_get_expressions(self, event: AstrMessageEvent):
        """
        获取 VTube Studio 当前模型的所有可用表情列表及其激活状态。
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            expressions = await self.vts.get_expressions()
            if not expressions:
                return "当前模型没有可用表情。"
            lines = ["当前模型可用表情："]
            for e in expressions:
                status = "✅ 激活中" if e.get("active") else "⬜ 未激活"
                lines.append(f"• {e.get('file', '?')} [{status}]")
            return "\n".join(lines)
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except Exception as e:
            return f"❌ 获取表情列表失败：{e}"

    @llm_tool(name="vts_move_model")
    async def tool_move_model(
        self,
        event: AstrMessageEvent,
        position_x: float = 0.0,
        position_y: float = 0.0,
        rotation: float = 0.0,
        size: float = 0.0,
        duration: float = 0.5,
    ):
        """
        移动、旋转或缩放 VTube Studio 中的 Live2D 模型。

        Args:
            position_x(number): 水平位置，范围 -1.0（最左）到 1.0（最右），0 为居中
            position_y(number): 垂直位置，范围 -1.0（最下）到 1.0（最上），0 为居中
            rotation(number): 旋转角度，范围 -360 到 360 度，0 为不旋转
            size(number): 缩放大小，范围 -100 到 100，0 为不变
            duration(number): 动画持续时间（秒），默认 0.5
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            await self.vts.move_model(
                position_x=position_x,
                position_y=position_y,
                rotation=rotation,
                size=size,
                time_in_seconds=duration,
            )
            return (
                f"✅ 已移动模型：位置({position_x:.2f}, {position_y:.2f}), "
                f"旋转{rotation}°, 大小变化{size}。"
            )
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except VTSTimeoutError as e:
            return f"❌ 请求超时：{e}"
        except Exception as e:
            return f"❌ 移动模型失败：{e}"

    @llm_tool(name="vts_inject_parameter")
    async def tool_inject_parameter(
        self,
        event: AstrMessageEvent,
        parameter_id: str,
        value: float,
        mode: str = "set",
    ):
        """
        向 VTube Studio 注入 Live2D 参数值，可以精细控制模型的面部表情参数。
        常用参数：FaceAngleX（水平转头）、FaceAngleY（点头）、FaceAngleZ（倾头）、
        MouthOpen（开嘴）、MouthSmile（微笑）、EyeOpenLeft/Right（眼睛睁开程度）。

        Args:
            parameter_id(string): 参数名称，例如 "MouthSmile" 或 "FaceAngleX"
            value(number): 参数值（通常为 -1.0 ~ 1.0）
            mode(string): 控制模式，"set" 表示直接设置，"add" 表示叠加，默认 "set"
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            await self.vts.inject_parameters(
                parameters=[{"id": parameter_id, "value": value}],
                mode=mode,
            )
            return f"✅ 已设置参数「{parameter_id}」= {value}（模式: {mode}）"
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except VTSTimeoutError as e:
            return f"❌ 请求超时：{e}"
        except Exception as e:
            return f"❌ 注入参数失败：{e}"

    @llm_tool(name="vts_get_parameters")
    async def tool_get_parameters(self, event: AstrMessageEvent):
        """
        获取 VTube Studio 当前模型所有可用的 Live2D 输入参数列表。
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            params = await self.vts.get_input_parameters()
            if not params:
                return "没有可用参数。"
            lines = [f"当前模型可用参数（共 {len(params)} 个，显示前30个）："]
            for p in params[:30]:
                lines.append(
                    f"• {p.get('name','?')} "
                    f"范围:[{p.get('min','?')}, {p.get('max','?')}] "
                    f"当前值:{p.get('value','?')}"
                )
            return "\n".join(lines)
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except Exception as e:
            return f"❌ 获取参数列表失败：{e}"

    @llm_tool(name="vts_model_info")
    async def tool_model_info(self, event: AstrMessageEvent):
        """
        获取 VTube Studio 当前加载的 Live2D 模型的基本信息。
        """
        err = await self._ensure_connection()
        if err:
            return err
        try:
            info = await self.vts.get_model_info()
            return (
                f"当前模型信息：\n"
                f"• 名称：{info.get('modelName', '未知')}\n"
                f"• 文件：{info.get('modelFileName', '未知')}\n"
                f"• VTS模型ID：{info.get('modelID', '未知')}"
            )
        except VTSConnectionError as e:
            self._connected = False
            return f"❌ 连接已断开：{e}"
        except Exception as e:
            return f"❌ 获取模型信息失败：{e}"



