"""关键词匹配：首字分桶 + 子串判定。

词表规模到几十万时单轮匹配仍是常数级——只有消息里出现过的首字对应的桶会被扫描，
耗时随「消息中不同首字个数 × 桶均长」增长，不随词表总量增长。

中文不能靠词边界判断：Python 的 \\w 含 CJK 会让整词匹配永不命中，JS 的 \\W 含 CJK
会让整词退化成子串。这里一律走子串，长词优先交给调用方按实体去重。
"""

from __future__ import annotations

from typing import Iterable, Iterator

# 多段文本（如同一轮里的连续几条消息）拼接时用的分隔符。裸拼接会让上段结尾的
# 「打大」和下段开头的「铁」拼出并不存在的「大铁」，所以每段独立匹配。
SEP = "\x01"

_MIN_LEN = 2


class Matcher:
    """按首字分桶的关键词索引。

    一个词条由主名和若干别名组成，命中任意一个都返回该词条的主名：
    「跳车」与「跳车狗」命中任一都返回 ``跳车狗``，调用方拿主名即可直接查库。
    """

    __slots__ = ("_buckets", "_seen", "_canon")

    def __init__(self) -> None:
        self._buckets: dict[str, list[str]] = {}
        self._seen: set[str] = set()
        self._canon: dict[str, list[str]] = {}

    def add(self, key: str, *aliases: str) -> None:
        key = key.strip()
        if len(key) < _MIN_LEN:
            return
        if key in self._canon:
            # 这个词先被别人当成别名登记过（「离经易道」既是心法也是几件装备的名字），
            # 主名仍要指回自己，且排在最前。
            keys = self._canon[key]
            if key not in keys:
                keys.insert(0, key)
        else:
            self._seen.add(key)
            self._canon[key] = [key]
            self._buckets.setdefault(key[0], []).append(key)
        for word in aliases:
            word = word.strip()
            if len(word) < _MIN_LEN:
                continue
            if word in self._canon:
                keys = self._canon[word]
                if key not in keys:
                    keys.append(key)
                continue
            self._seen.add(word)
            self._canon[word] = [key]
            self._buckets.setdefault(word[0], []).append(word)

    def extend(self, rows: Iterable[tuple[str, ...]]) -> None:
        for row in rows:
            self.add(*row)

    def find(self, text: str, limit: int | None = None) -> list[str]:
        """返回命中的词条主名，按命中词的字长降序。

        不在这里丢弃「短词是长词子串」的情况：词表里可能有「天策」和「天策府」这种
        互不包含的不同实体，丢错比留多更糟。同一词条的主名与别名命中后在此归一。
        """
        segments = [s for s in text.split(SEP) if s]
        if not segments:
            return []

        hits: list[str] = []
        # ponytail: 单字分桶，30 万词实测 0.68 ms/轮，够群聊用。首字分布极不均匀的
        # 词表（某个常用字下几千词）会退化到几毫秒，届时改双字前缀桶。
        for ch in set(text):
            for word in self._buckets.get(ch, ()):
                if any(word in seg for seg in segments):
                    hits.append(word)

        hits.sort(key=lambda w: (-len(w), w))

        out: list[str] = []
        for word in hits:
            for key in self._canon[word]:
                if key not in out:
                    out.append(key)
                    if limit is not None and len(out) >= limit:
                        return out
        return out

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, word: str) -> bool:
        return word in self._seen

    def __iter__(self) -> Iterator[str]:
        return iter(self._seen)


def _self_check() -> None:
    m = Matcher()
    m.add("低保")
    m.add("大铁", "玄晶")
    m.add("黑工")
    m.add("包团")
    m.add("跳车狗", "跳车")
    m.add("紫霞功", "紫霞", "紫霞心法")
    m.add("天策")
    m.add("天策府")
    m.add("单")  # 短于 2 字，不入桶

    # 真实群聊句
    got = m.find("这周低保出了个垃圾，大铁三十万起拍，我黑工已经打了两车")
    assert got[:3] == ["低保", "大铁", "黑工"], got  # 同长按码点序

    # 别名能接住聊天里的简称
    assert "跳车狗" in m.find("长安城门口集合，别跳车啊")

    # 主名与别名命中后归一到同一词条
    assert m.find("紫霞心法这版本还能玩吗") == ["紫霞功"]

    # 高频寒暄不命中
    assert m.find("今天晚饭吃啥") == []
    assert m.find("") == []

    # 跨段不假命中
    assert m.find("打大" + SEP + "铁") == []
    assert "大铁" in m.find("打大" + SEP + "铁大铁")

    # 长词优先，但不同实体都保留
    assert m.find("天策府门口") == ["天策府", "天策"]

    # 单字不成词
    assert "单" not in m

    # limit 截断
    assert len(m.find("低保大铁黑工包团", limit=2)) == 2

    print(f"matcher self-check ok, {len(m)} words")


if __name__ == "__main__":
    _self_check()
