"""AstrBot 插件：聊天里碰到剑网3的东西，就把对应的正确含义递给模型。

命不中就不注入；同一会话连续几轮命中同一批条目时也不重复注入。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:
    from .core.inject import render
    from .core.store import Store
except ImportError:  # 顶层导入（自检、直接运行）
    from core.inject import render
    from core.store import Store

try:
    # 4.29 起 TextPart 才有 mark_as_temp：只对本次请求生效，不写进对话历史。
    from astrbot.core.agent.message import TextPart
except Exception:  # noqa: BLE001 - 旧版没有这个模块时退回改 system_prompt
    TextPart = None

logger = logging.getLogger("astrbot.plugin.jx3_memory")

PLUGIN_NAME = "astrbot_plugin_jx3_memory"
DB_NAME = "memory.sqlite3"
MAX_SESSIONS = 500


def _as_int(config: Any, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


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
        self._seen: dict[str, set[str]] = {}

        configured = str(self.config.get("db_path") or "").strip()
        path = Path(configured) if configured else Path(__file__).parent / "data" / DB_NAME
        self.store: Store | None = None
        try:
            self.store = Store(path)
            logger.info("jx3 memory: %s words loaded from %s", len(self.store), path)
        except Exception as exc:  # noqa: BLE001 - 库缺失不该让整个插件加载失败
            logger.warning("jx3 memory: 记忆库不可用（%s），本轮起不注入: %s", path, exc)

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if self.store is None:
            return

        texts = [req.prompt or ""]
        for ctx in (req.contexts or [])[-self.recent :] if self.recent else []:
            text = _text_of(ctx)
            if text:
                texts.append(text)

        hits = self.store.search(texts, limit=self.max_items)
        if not hits:
            return

        key = str(getattr(event, "unified_msg_origin", "") or "")
        names = {name for name, _ in hits}
        prev = self._seen.get(key)
        if prev is not None and names <= prev:
            return
        if prev is None and len(self._seen) >= MAX_SESSIONS:
            self._seen.clear()
        self._seen[key] = names

        block = render(hits, max_items=self.max_items, max_chars=self.max_chars)
        if not block:
            return
        if TextPart is not None:
            req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
        else:
            req.system_prompt = (req.system_prompt or "") + "\n" + block
