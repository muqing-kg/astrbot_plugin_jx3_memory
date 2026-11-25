"""AstrBot 插件：聊天里碰到剑网3的东西，就把对应的正确含义递给模型。

命不中就不注入；命中了每轮都给，注入本身不进历史。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:
    from .core.inject import render
    from .core.store import Store
    from .core.sync import sync as sync_news
except ImportError:  # 顶层导入（自检、直接运行）
    from core.inject import render
    from core.store import Store
    from core.sync import sync as sync_news

try:
    # 4.29 起 TextPart 才有 mark_as_temp：只对本次请求生效，不写进对话历史。
    from astrbot.core.agent.message import TextPart
except Exception:  # noqa: BLE001 - 没有就不注入，绝不退回 system_prompt
    TextPart = None

# logger 用 AstrBot 那个，不要自己 logging.getLogger：AstrBot 的 Formatter 要求
# record 带 plugin_tag，自己建的 logger 不过它的 Filter，一条日志就能把插件加载搞崩。

PLUGIN_NAME = "astrbot_plugin_jx3_memory"
DB_NAME = "memory.sqlite3"
SYNC_HOUR = 3

# 句子里出现这些词，说明是在问做法而不是随口一提，才值得把正文掏出来
ASK_WORDS = (
    "怎么", "如何", "怎样", "在哪", "哪里", "流程", "攻略", "步骤",
    "怎么做", "怎么打", "怎么获得", "怎么拿", "怎么办", "触发",
    "是什么", "什么用", "什么效果", "干嘛", "作用", "属性", "区别",
    "多少", "为什么", "介绍", "说明",
)


def _as_int(config: Any, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _seconds_until(hour: int) -> float:
    """距下一个整点的秒数；已经过了就算明天。"""
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _text_of(ctx: Any) -> str:
    """历史消息取纯文本；多模态的图片、音频块直接丢掉。"""
    if not isinstance(ctx, dict):
        return ""
    content = ctx.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


class JX3MemoryPlugin(Star):
    def __init__(self, context: Context, config: Any | None = None) -> None:
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.max_items = max(1, _as_int(self.config, "max_items", 5))
        self.max_chars = max(200, _as_int(self.config, "max_chars", 1200))
        self.recent = max(0, _as_int(self.config, "recent_messages", 3))
        self.proxy = str(self.config.get("proxy") or "").strip()
        self.api = str(self.config.get("api_base") or "").strip()

        configured = str(self.config.get("db_path") or "").strip()
        self.db_path = Path(configured) if configured else Path(__file__).parent / "data" / DB_NAME
        self.store: Store | None = None
        try:
            self.store = Store(self.db_path)
            logger.info("jx3 memory: %s words loaded from %s", len(self.store), self.db_path)
        except Exception as exc:  # noqa: BLE001 - 库缺失不该让整个插件加载失败
            logger.warning("jx3 memory: 记忆库不可用（%s），本轮起不注入: %s", self.db_path, exc)

        self._sync_task: asyncio.Task | None = None
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # 没有事件循环时只丢掉定时，注入照常
            logger.warning("jx3 memory: 无事件循环，公告定时同步未启动")
        else:
            self._sync_task = asyncio.create_task(self._daily_sync())

    async def _daily_sync(self) -> None:
        """每天 SYNC_HOUR 点拉一次最新公告；库里的公告按 id 去重、正文变了才更新。"""
        while True:
            await asyncio.sleep(_seconds_until(SYNC_HOUR))
            try:
                added, updated = await asyncio.to_thread(
                    sync_news, self.db_path, self.proxy, api=self.api
                )
                logger.info("jx3 memory: 公告同步完成，新增 %s 条、更新 %s 条", added, updated)
                if (added or updated) and self.store is not None:
                    await asyncio.to_thread(self.store.reload)
            except Exception as exc:  # noqa: BLE001 - 同步失败不该拖垮插件
                logger.warning("jx3 memory: 公告同步失败: %s", exc)

    async def terminate(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            self._sync_task = None
        if self.store is not None:
            self.store.close()

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if self.store is None or TextPart is None:
            return
        # 没有这个字段的版本就别注入，宁可不给也不能退回系统提示词
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            return

        # 注入只是锦上添花，出了任何差错都不能连累这条消息本身的回复
        try:
            texts = [req.prompt or ""]
            for ctx in (req.contexts or [])[-self.recent :] if self.recent else []:
                text = _text_of(ctx)
                if text:
                    texts.append(text)

            hits = self.store.search(texts, limit=self.max_items)
            if not hits:
                return

            detail = None
            if any(w in (req.prompt or "") for w in ASK_WORDS):
                for name, _ in hits:
                    got = self.store.detail(name)
                    if got:
                        detail = (name, got[1])
                        break

            block = render(
                hits, max_items=self.max_items, max_chars=self.max_chars, detail=detail
            )
            if block:
                parts.append(TextPart(text=block).mark_as_temp())
        except Exception as exc:  # noqa: BLE001
            logger.warning("jx3 memory: 本轮注入失败，跳过: %s", exc)
