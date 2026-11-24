"""把命中条目拼成交给模型的一段话。

这不是资料展示，是为了让模型别硬编：给它几个词的正确含义，剩下的靠自己。
所以不编号、不标来源、不说「以下是检索结果」，也不提示条数。
"""

from __future__ import annotations

HEAD = (
    "（你玩了很久剑网3，下面这些是你本来就知道的东西。用得上的时候用自己的话说，"
    "不要照抄，也不要提「资料」「条目」这类词。没提到的说明你不清楚，别猜。）"
)

MAX_ITEMS = 5
MAX_CHARS = 1200


def render(
    items: list[tuple[str, str]],
    max_items: int = MAX_ITEMS,
    max_chars: int = MAX_CHARS,
) -> str:
    """items 为 [(名字, 摘要)]，摘要在库内已截短。返回空串表示本轮不注入。"""
    lines: list[str] = []
    used = len(HEAD)
    for name, brief in items:
        brief = (brief or "").strip()
        if not brief:
            continue
        line = f"· {name}：{brief}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
        if len(lines) >= max_items:
            break
    if not lines:
        return ""
    return HEAD + "\n" + "\n".join(lines)


def _self_check() -> None:
    assert render([]) == ""

    one = render([("大铁", "玄晶的俗称")])
    assert one.startswith(HEAD)
    assert "· 大铁：玄晶的俗称" in one

    # 摘要空条目不算数，全空则不注入
    assert render([("x", ""), ("y", "   ")]) == ""

    # 条数上限
    many = [(f"名字{i}", "释义") for i in range(20)]
    assert render(many, max_items=3).count("·") == 3

    # 字数上限：最后一条塞不下就整条丢掉，不截半句
    tight = render([("甲", "一" * 50), ("乙", "二" * 50)], max_chars=len(HEAD) + 60)
    assert "甲" in tight and "乙" not in tight

    print("inject self-check ok")


if __name__ == "__main__":
    _self_check()
