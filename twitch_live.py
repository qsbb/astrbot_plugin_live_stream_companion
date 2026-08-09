"""Twitch 直播弹幕监听（匿名 IRC，无需 OAuth token）。

Twitch IRC 允许匿名连接（justinfan 用户名）读取公开频道的聊天弹幕，
因此本模块不依赖任何第三方库或 token，仅使用 asyncio + ssl 标准库即可工作。

事件结构复用 bilibili_live.LiveDanmakuEvent，raw 中标记 platform="twitch"，
方便主插件以统一的 LiveDanmakuEvent 语义处理 Twitch 弹幕。
"""

import asyncio
import re
import ssl
import time
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .bilibili_live import LiveDanmakuEvent

TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697
TWITCH_ANON_USERNAME = "justinfan12345"
TWITCH_ANON_PASSWORD = "SCHMOOPIIE"
TWITCH_READ_TIMEOUT_SECONDS = 240.0

# 示例弹幕行：
# @badge-info=...;display-name=SomeUser;user-id=12345;... :someuser!someuser@someuser.tmi.twitch.tv PRIVMSG #channel :hello world
_PRIVMSG_RE = re.compile(
    r"^@([^ ]+) :([^!]+)![^ ]+ PRIVMSG #([^ ]+) :(.*)$",
    re.DOTALL,
)


def _unescape_tag(value: str) -> str:
    """Twitch IRC tags 使用 \\s 表示空格、\\: 表示分号、\\\\ 表示反斜杠。"""
    return (
        str(value)
        .replace(r"\s", " ")
        .replace(r"\:", ";")
        .replace(r"\\", "\\")
    )


def _parse_tags(tags_text: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for item in str(tags_text or "").split(";"):
        if not item:
            continue
        if "=" in item:
            key, _, value = item.partition("=")
        else:
            key, value = item, ""
        tags[key] = value
    return tags


class TwitchIrcClient:
    """Twitch 匿名 IRC 弹幕客户端，带断线自动重连。"""

    def __init__(
        self,
        channel: str,
        on_event: Callable[[LiveDanmakuEvent], Awaitable[None]],
        *,
        reconnect_interval: float = 5.0,
        debug_log: bool = False,
    ) -> None:
        self.channel = str(channel or "").strip().lower().lstrip("#")
        self.on_event = on_event
        self.reconnect_interval = max(1.0, float(reconnect_interval))
        self.debug_log = bool(debug_log)
        self.last_error = ""
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def start(self) -> asyncio.Task:
        if self.is_running:
            return self._task
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        task = self._task
        self._task = None
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _run(self) -> None:
        while not (self._stop_event and self._stop_event.is_set()):
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                logger.warning(f"[Twitch] 弹幕监听连接异常，{self.reconnect_interval}s 后重连: {e}")
            if self._stop_event and self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.reconnect_interval)
            except asyncio.TimeoutError:
                continue

    async def _connect_once(self) -> None:
        context = ssl.create_default_context()
        reader, writer = await asyncio.open_connection(
            TWITCH_IRC_HOST, TWITCH_IRC_PORT, ssl=context
        )
        self.last_error = ""
        try:
            writer.write(f"CAP REQ :twitch.tv/tags\r\n".encode("utf-8"))
            writer.write(f"PASS {TWITCH_ANON_PASSWORD}\r\n".encode("utf-8"))
            writer.write(f"NICK {TWITCH_ANON_USERNAME}\r\n".encode("utf-8"))
            writer.write(f"JOIN #{self.channel}\r\n".encode("utf-8"))
            await writer.drain()
            if self.debug_log:
                logger.info(f"[Twitch] 已连接频道 #{self.channel}（匿名 IRC）")
            while not (self._stop_event and self._stop_event.is_set()):
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=TWITCH_READ_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    # 长时间无数据，主动 ping 保活
                    writer.write(b"PING :tmi.twitch.tv\r\n")
                    await writer.drain()
                    continue
                if not line:
                    logger.warning("[Twitch] 连接被服务器关闭，准备重连")
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if self.debug_log:
                    logger.debug(f"[Twitch] << {text}")
                if text.startswith("PING"):
                    writer.write(f"PONG :{text[5:]}\r\n".encode("utf-8"))
                    await writer.drain()
                    continue
                event = self._parse_line(text)
                if event is not None and self.on_event is not None:
                    try:
                        await self.on_event(event)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[Twitch] 事件回调失败: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _parse_line(self, line: str) -> LiveDanmakuEvent | None:
        """解析一行 IRC 消息。非弹幕行返回 None。"""
        if not line or not line.startswith("@"):
            return None
        m = _PRIVMSG_RE.match(line)
        if not m:
            return None
        tags_text, login, channel, message = m.groups()
        tags = _parse_tags(tags_text)
        display_name = _unescape_tag(tags.get("display-name", "")) or login
        user_id = _unescape_tag(tags.get("user-id", ""))
        content = str(message or "").strip()
        if not content:
            return None
        return LiveDanmakuEvent(
            event_type="danmaku",
            username=display_name,
            content=content,
            raw={
                "platform": "twitch",
                "channel": channel,
                "login": login,
                "user_id": user_id,
                "tags": tags,
            },
        )
