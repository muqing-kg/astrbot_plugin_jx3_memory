"""把命中条目拼成交给模型的一段话。

这不是资料展示，是为了让模型别硬编：给它几个词的正确含义，剩下的靠自己。
所以不编号、不标来源、不说「以下是检索结果」，也不提示条数。
"""

from __future__ import annotations

import re

# 上游正文里混着零宽空格之类的不可见字符，念出来看不出、占位置、还会被模型照抄
_ZW = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
# 玩家写的攻略爱用横线、菱形分隔段落，四百字里能有半屏是分隔线
_RULE = re.compile(r"[-=_*~—－◆※●▪▼★☆]{2,}")

HEAD = (
    "（下面这些是剑网3 里的东西，给你听懂对方在说什么用的。"
    "回复时不必提到它们，也不要照搬里面的说法；"
    "对方没问就别主动讲，问到了别说错。没列到的说明你不清楚，别猜。"
)
TAIL = "）"

MAX_ITEMS = 5
MAX_CHARS = 1200
# 攻略的流程是操作手册，整段念出来就成了复述。一百二十字刚好停在「前置条件 + 触发方式」。
DETAIL_CHARS = 120


def render(
    items: list[tuple[str, str]],
    max_items: int = MAX_ITEMS,
    max_chars: int = MAX_CHARS,
    detail: tuple[str, str] | None = None,
) -> str:
    """items 为 [(名字, 摘要)]，摘要在库内已截短。返回空串表示本轮不注入。

    detail 是问「怎么做」时那一条的正文，排在摘要前面，因为它才是真答案。
    整块连成一段、不换行：一行一条的列表模型会照着排版，回话也分行分点。
    """
    parts: list[str] = []
    picked: list[str] = []
    used = len(HEAD) + len(TAIL)

    if detail:
        name, body = detail
        # 帖子正文里有存成字面两字符的反斜杠 n，直接注进去会显得很怪
        body = " ".join(_ZW.sub("", body or "").replace("\\n", " ").split())
        body = " ".join(_RULE.sub(" ", body).split())
        if body:
            part = f"{name}：{body[:DETAIL_CHARS]}"
            if used + len(part) + 1 <= max_chars:
                parts.append(part)
                picked.append(name)
                used += len(part) + 1
        items = [(n, b) for n, b in items if n != name]

    for name, brief in items:
        brief = _ZW.sub("", brief or "").strip()
        if not brief:
            continue
        # 名字里带已选条目名的，是同一件事的细分（「万花·离经易道·竞技对抗」之于
        # 「离经易道」），念出来只会挤掉别的条目
        if any(n in name for n in picked):
            continue
        part = f"{name}：{brief}"
        if used + len(part) + 1 > max_chars:
            break
        parts.append(part)
        picked.append(name)
        used += len(part) + 1
        if len(parts) >= max_items:
            break
    if not parts:
        return ""
    return HEAD + "；".join(parts) + TAIL


def _self_check() -> None:
    assert render([]) == ""

    one = render([("大铁", "玄晶的俗称")])
    assert one.startswith(HEAD) and one.endswith(TAIL)
    assert "大铁：玄晶的俗称" in one
    # 整块只有一行：列表排版会被模型学去，回话也分行
    assert "\n" not in one

    # 摘要空条目不算数，全空则不注入
    assert render([("x", ""), ("y", "   ")]) == ""

    # 条数上限
    many = [(f"名字{i}", "释义") for i in range(20)]
    assert render(many, max_items=3).count("名字") == 3

    # 字数上限：最后一条塞不下就整条丢掉，不截半句
    tight = render([("甲", "一" * 50), ("乙", "二" * 50)],
                   max_chars=len(HEAD) + len(TAIL) + 60)
    assert "甲" in tight and "乙" not in tight

    # 同一件事的细分不重复占位
    sub = render([("离经易道", "万花心法"), ("万花·离经易道·竞技对抗", "一件装备")])
    assert "万花·离经易道" not in sub and "离经易道：万花心法" in sub

    # 问「怎么做」时那条的正文排在摘要前面
    with_detail = render([("大铁", "玄晶的俗称")], detail=("三山四海", "第一幕 挖宝逢奇缘"))
    assert "三山四海：第一幕" in with_detail
    assert with_detail.index("三山四海") < with_detail.index("大铁")

    # 上游正文里的零宽字符不念给模型
    assert "\u200b" not in render([("包团", "包团老板\u200b，指副本团")])
    assert "\u200b" not in render([], detail=("正文", "包团\u200b老板"))

    print("inject self-check ok")


if __name__ == "__main__":
    _self_check()
