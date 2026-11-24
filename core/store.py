"""记忆库读取：把 entry 与 term 装进 Matcher，命中原词返回条目摘要。

同名跨类型只留一个（门派名同时是技能名时以门派为准），取舍顺序与入库脚本一致。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

try:
    from .matcher import SEP, Matcher
except ImportError:  # 顶层导入（自检、直接运行）
    from matcher import SEP, Matcher

PRIO = {
    "school": 0,
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
        self.reload()

    def reload(self) -> None:
        """重装词表。公告同步后调用，换掉整个 Matcher 而不是往里加。"""
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.matcher = Matcher()
        self._load()

    def _load(self) -> None:
        rows = self.db.execute("SELECT id, kind, name, brief FROM entry").fetchall()
        rows.sort(key=lambda r: (PRIO.get(r["kind"], 99), r["id"]))
        win: dict[str, sqlite3.Row] = {}
        for r in rows:
            win.setdefault(r["name"], r)
        self._brief = {n: _clip(r["brief"] or "") for n, r in win.items()}

        name_of = {r["id"]: r["name"] for r in rows}
        live = {r["id"] for r in win.values()}
        aliases: dict[str, list[str]] = {}
        for word, eid in self.db.execute("SELECT word, entry_id FROM term"):
            if eid in live:
                name = name_of[eid]
                if name in win and win[name]["id"] == eid:
                    aliases.setdefault(name, []).append(word)
        for name, words in aliases.items():
            floor = 3 if win[name]["kind"] in SHORT_KINDS else 2
            keep = [w for w in words if len(w) >= floor]
            if len(name) >= floor:
                self.matcher.add(name, *keep)
        self.db.close()

    def search(self, texts: list[str], limit: int = 5) -> list[tuple[str, str]]:
        """返回 [(名字, 摘要)]，长词优先；摘要为空的条目跳过。"""
        out: list[tuple[str, str]] = []
        for name in self.matcher.find(SEP.join(texts)):
            brief = self._brief.get(name)
            if not brief:
                continue
            out.append((name, brief))
            if len(out) >= limit:
                break
        return out

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
        print(f"store self-check ok, {len(s)} words")


if __name__ == "__main__":
    _self_check()
