"""记忆库读取：把 entry 与 term 装进 Matcher，命中原词返回条目摘要。

同名跨类型只留一个（门派名同时是技能名时以门派为准），取舍顺序与入库脚本一致。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
try:
    from .matcher import SEP, Matcher
except ImportError:  # 顶层导入（自检、直接运行）
    from matcher import SEP, Matcher

DETAIL_NAME = "memory_detail.sqlite3"
ATTR_NAME = "memory_attr.sqlite3"
MIN_DETAIL = 20

# 条目类目 → 正文库/属性库里对应的类目。不在表里的类目没有正文可取。
_DK = {
    "quest": "quest",
    "serendipity": "serendipity",
    "achievement": "post",
    "item": "post",
    "knowledge": "post",
    "skill": "post",
    "xf": "post",
}
_AK = {"item": "item", "skill": "skill", "xf": "talent"}

_PUNCT = re.compile(r"[！!·・—－\-：:、，,（）()\[\]【】]+")


def _cover(name: str, segments: list[str]) -> int:
    """名字按标点拆段后，各段是否在同一段消息里凑齐。2 表示可当整条命中。

    跨消息凑的不算：上一条提「万花」、这一条提「离经易道」，说的不一定是一回事。
    """
    segs = [s for s in _PUNCT.split(name) if len(s) >= 2]
    if len(segs) < 2:
        return 2
    return 2 if any(all(s in seg for s in segs) for seg in segments) else 1

PRIO = {
    "xf": 1,
    "skill": 2,
    "dungeon": 3,
    "boss": 4,
    "serendipity": 5,
    "knowledge": 6,
    "achievement": 7,
    "quest": 8,
    "news": 9,
    "item": 10,
    "dlc": 6,
    "map": 6,
    "npc": 6,
    "buff": 5,
    "pet": 7,
}

# 物品、任务、成就里的两字名几乎都是通用词（「长安」「龙渊」既是地名也是任务名），
# 注进去比不给还糟。这三类要求三字以上才进词表，其余类别两字即可。
SHORT_KINDS = frozenset({"item", "quest", "achievement"})

BRIEF_CHARS = 60
_STRONG = "。！？；"
_WEAK = "，、"


def _clip(text: str, n: int = BRIEF_CHARS) -> str:
    """摘要按句读收口，不把半句话留给模型。"""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    head = text[:n]
    cut = max(head.rfind(c) for c in _STRONG)
    if cut >= n // 2:
        return head[: cut + 1]
    cut = max(head.rfind(c) for c in _WEAK)
    if cut >= n // 2:
        return head[:cut] + "…"
    return head + "…"


class Store:
    """词表与条目摘要常驻内存，查库只发生在启动时。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.matcher = Matcher()
        self._brief: dict[str, str] = {}
        self._kind: dict[str, str] = {}
        self._detail: sqlite3.Connection | None = None
        dpath = self.path.parent / DETAIL_NAME
        apath = self.path.parent / ATTR_NAME
        if dpath.is_file():
            self._detail = sqlite3.connect(
                f"file:{dpath}?mode=ro", uri=True, check_same_thread=False
            )
            if apath.is_file():
                self._detail.execute(
                    "ATTACH DATABASE ? AS attr", (f"file:{apath}?mode=ro",)
                )
        self.reload()

    def reload(self) -> None:
        """重装词表。公告同步后调用，换掉整个 Matcher 而不是往里加。"""
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._load()

    def _load(self) -> None:
        rows = self.db.execute("SELECT id, kind, name, brief FROM entry").fetchall()
        rows.sort(key=lambda r: (PRIO.get(r["kind"], 99), r["id"]))
        win: dict[str, sqlite3.Row] = {}
        for r in rows:
            win.setdefault(r["name"], r)
        brief = {n: _clip(r["brief"] or "") for n, r in win.items()}
        kind = {n: r["kind"] for n, r in win.items()}

        name_of = {r["id"]: r["name"] for r in rows}
        live = {r["id"] for r in win.values()}
        aliases: dict[str, list[str]] = {}
        for word, eid in self.db.execute("SELECT word, entry_id FROM term"):
            if eid in live:
                name = name_of[eid]
                if name in win and win[name]["id"] == eid:
                    aliases.setdefault(name, []).append(word)
        matcher = Matcher()
        for name, words in aliases.items():
            floor = 3 if win[name]["kind"] in SHORT_KINDS else 2
            keep = [w for w in words if len(w) >= floor]
            if len(name) >= floor:
                matcher.add(name, *keep)
        self.db.close()
        # 建完再整体换上：重装期间的搜索继续用旧词表，不会出现空窗
        self.matcher, self._brief, self._kind = matcher, brief, kind

    def search(self, texts: list[str], limit: int = 5) -> list[tuple[str, str]]:
        """返回 [(名字, 摘要)]，长词优先；摘要为空的条目跳过。"""
        segments = [t for t in texts if t]
        joined = SEP.join(segments)
        # 不在这里限条数：长名（「万花·离经易道·竞技对抗（234品）」）先出，
        # 想要的那条短名在心法里，截断会把正确答案挤掉。
        hits = [(n, self._brief.get(n, "")) for n in self.matcher.find(joined)]
        # 名字被标点断开时（「傲意战魂！第十阶」），消息里各段都出现才算真命中；
        # 只中一段的往后排，否则问「第十阶」会给出「第一阶」的攻略。
        # 同分再按类目优先级：问「离经易道」要的是心法，不是那件同名的装备。
        hits.sort(
            key=lambda h: (
                -_cover(h[0], segments),
                PRIO.get(self._kind.get(h[0], ""), 99),
                -len(h[0]),
            )
        )
        return [(n, b) for n, b in hits if b][:limit]

    def detail(self, name: str) -> tuple[str, str] | None:
        """按名字取正文，返回 (类目, 正文)；没有正文库或没这条就 None。

        名字会撞车：任务「龙渊泽」和副本「龙渊泽」同名，只按名字查会取到任务的
        背景故事，问「老三怎么打」就答成剧情。所以按条目类目限定来源，对不上的
        类目干脆不给——只留摘要，比给错的正文强。
        """
        if self._detail is None:
            return None
        kind = self._kind.get(name, "")
        try:
            dk = _DK.get(kind)
            if dk:
                row = self._detail.execute(
                    "SELECT body FROM detail WHERE name=? AND kind=?"
                    " ORDER BY length(body) DESC LIMIT 1",
                    (name, dk),
                ).fetchone()
                if row and len(row[0]) >= MIN_DETAIL:
                    return (dk, row[0])
            ak = _AK.get(kind)
            if ak:
                row = self._detail.execute(
                    "SELECT body FROM attr WHERE name=? AND kind=?"
                    " ORDER BY length(body) DESC LIMIT 1",
                    (name, ak),
                ).fetchone()
                if row and len(row[0]) >= MIN_DETAIL:
                    return (ak, row[0])
            # 副本、地图这类自己没有正文——打法写在别人的攻略标题里（「龙渊泽·25人挑战攻略」）。
            # 精确查不到就按标题包含再找一次，否则问「XX 怎么打」只能答一句摘要。
            # 标题以它开头的优先，其次才看正文长短，免得挑中最啰嗦的那篇。
            if len(name) >= 3:
                row = self._detail.execute(
                    "SELECT body FROM detail WHERE kind='post' AND name LIKE ?"
                    " ORDER BY (name LIKE ?) DESC, length(body) DESC LIMIT 1",
                    (f"%{name}%", f"{name}%"),
                ).fetchone()
                if row and len(row[0]) >= MIN_DETAIL:
                    return ("post", row[0])
        except sqlite3.Error:
            return None
        return None

    def close(self) -> None:
        """放掉正文库句柄。Windows 上不关，临时目录和库文件都删不掉。"""
        if self._detail is not None:
            try:
                self._detail.close()
            except sqlite3.Error:
                pass
            self._detail = None

    def __len__(self) -> int:
        return len(self.matcher)


def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE entry (id INTEGER PRIMARY KEY, kind TEXT, name TEXT, brief TEXT, meta TEXT, body TEXT);
            CREATE TABLE term (word TEXT, entry_id INTEGER, PRIMARY KEY (word, entry_id)) WITHOUT ROWID;
            INSERT INTO entry VALUES (1,'knowledge','大铁','玄晶的俗称','','');
            INSERT INTO entry VALUES (2,'item','玄晶','镶嵌用的高级材料','','');
            INSERT INTO entry VALUES (3,'dungeon','长安','长安城','','');
            INSERT INTO entry VALUES (4,'item','长安','同名物品，应被副本压掉','','');
            INSERT INTO entry VALUES (5,'item','空条目','','','');
            INSERT INTO entry VALUES (6,'quest','洛道','两字任务名，不入词表','','');
            INSERT INTO entry VALUES (7,'knowledge','长句','甲' || hex(randomblob(200)) || '。','','');
            INSERT INTO term VALUES ('大铁',1),('玄晶',1),('玄晶',2),('长安',3),('长安',4),('空条目',5),('洛道',6),('长句',7);
            """
        )
        db.commit()
        db.close()

        det = sqlite3.connect(Path(tmp) / DETAIL_NAME)
        det.executescript(
            """
            CREATE TABLE detail (kind TEXT, name TEXT, body TEXT, meta TEXT, PRIMARY KEY (kind, name));
            INSERT INTO detail VALUES ('quest','龙渊泽','任务正文：一段与副本无关的背景故事，够长够长够长。','');
            INSERT INTO detail VALUES ('post','龙渊泽·25人挑战攻略','副本打法正文：老一门口集合，老二躲红圈，够二十个字了。','');
            INSERT INTO detail VALUES ('post','玄晶','玩家写的攻略正文，讲玄晶怎么来，够二十个字了。','');
            """
        )
        det.commit()
        det.close()

        s = Store(path)
        assert len(s) > 0
        assert s.search(["大铁三十万起拍"]) == [("大铁", "玄晶的俗称")]
        # 两字物品名不单独成条，但仍能经由别人的别名命中
        assert s.search(["玄晶"]) == [("大铁", "玄晶的俗称")]
        assert s.search(["玄晶"])[0][0] != "玄晶"
        # 两字任务名不入词表
        assert "洛道" not in s.matcher
        # 同名跨类型取优先级高者
        assert s.search(["长安城门口"]) == [("长安", "长安城")]
        # 摘要为空不入结果，空消息不命中
        assert s.search([""]) == []
        # 多段文本之间不跨段拼词
        assert s.search(["打大", "铁"]) == []
        # 摘要按句读截短，不留半句
        long_brief = s.search(["长句"])[0][1]
        assert len(long_brief) <= BRIEF_CHARS + 1 and long_brief[-1] in "。…"
        # 名字撞车按类目取正文：副本「龙渊泽」不该拿到同名任务的剧情，
        # 精确查不到时退一步拿标题里带它的攻略
        got = s.detail("龙渊泽")
        assert got is not None and got[1].startswith("副本打法"), got
        # 类目对得上就照常取
        assert s.detail("玄晶")[1].startswith("玩家写的攻略")
        s.close()
        print(f"store self-check ok, {len(s)} words")


if __name__ == "__main__":
    _self_check()
