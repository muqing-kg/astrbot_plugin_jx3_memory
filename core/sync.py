"""公告增量同步：拉 JX3API 最新公告，按 id 去重、正文变了才更新，同时维护检索表。

只碰 kind='news' 的条目，不重建全库。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://www.jx3api.com"
UA = "Mozilla/5.0 astrbot-plugin-jx3-memory"
LIMIT = 50
BRIEF_CHARS = 120

_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_BLANK = re.compile(r"[ \t\u3000]+")
_NL = re.compile(r"\n{3,}")
_JUNK = re.compile(r"[·・\s]+")


def clean(raw: str) -> str:
    if not raw:
        return ""
    text = _DROP.sub(" ", raw)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text, flags=re.I)
    text = _TAG.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = _BLANK.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL.sub("\n\n", text).strip()


def to_time(desc: dict, date_str: str) -> str:
    try:
        return datetime.fromtimestamp(int(desc.get("inputtime")), tz=timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError):
        pass
    return ((desc.get("asktime") or "").strip() or (date_str or "").strip()).replace("/", "-")


def fetch(proxy: str = "", limit: int = LIMIT, api: str = API) -> list[dict]:
    url = f"{(api or API).rstrip('/')}/news/records?limit={max(1, min(LIMIT, int(limit)))}"
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    with urllib.request.build_opener(*handlers).open(request, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != 200:
        raise RuntimeError(f"JX3API 返回 {payload.get('status')}: {payload.get('detail')}")
    return payload.get("detail") or []


def _put_entry(con: sqlite3.Connection, title: str, body: str) -> None:
    """公告同时进检索表，标题即主名，正文首段作摘要。"""
    brief = " ".join(body.split())[:BRIEF_CHARS]
    # INSERT OR REPLACE 会换掉旧行、留下孤儿 term，先清干净
    for (old_id,) in con.execute("SELECT id FROM entry WHERE kind='news' AND name=?", (title,)).fetchall():
        con.execute("DELETE FROM term WHERE entry_id=?", (old_id,))
    con.execute(
        "INSERT OR REPLACE INTO entry(kind, name, brief, meta) VALUES('news', ?, ?, '')",
        (title, brief),
    )
    row = con.execute("SELECT id FROM entry WHERE kind='news' AND name=?", (title,)).fetchone()
    if not row:
        return
    eid = row[0]
    con.execute("DELETE FROM term WHERE entry_id=?", (eid,))
    for word in {title, _JUNK.sub("", title)}:
        if len(word) >= 2:
            con.execute("INSERT OR IGNORE INTO term(word, entry_id) VALUES(?, ?)", (word, eid))


def sync(db_path: str | Path, proxy: str = "", limit: int = LIMIT, api: str = API) -> tuple[int, int]:
    """返回 (新增, 更新)。正文没变的条目只刷新抓取时间，不动检索表。"""
    items = fetch(proxy, limit, api)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    added = updated = 0

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS news (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT, published_at TEXT,
                url TEXT, body TEXT, body_hash TEXT, fetched_at TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_news_time ON news(published_at DESC)")
        for item in items:
            desc = item.get("desc") or {}
            nid = str(desc.get("id") or item.get("catid") or item.get("url") or "")
            title = (item.get("title") or "").strip()
            if not nid or not title:
                continue
            body = clean(desc.get("content") or "")
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            old = con.execute("SELECT body_hash FROM news WHERE id=?", (nid,)).fetchone()
            con.execute(
                "INSERT OR REPLACE INTO news(id, title, kind, published_at, url, body, body_hash, fetched_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    nid,
                    title,
                    item.get("type") or "",
                    to_time(desc, item.get("date") or ""),
                    item.get("url") or "",
                    body,
                    digest,
                    now,
                ),
            )
            if old is None:
                added += 1
            elif old[0] != digest:
                updated += 1
            else:
                continue
            _put_entry(con, title, body)
        con.commit()
    finally:
        con.close()
    return added, updated


def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.sqlite3"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE entry(id INTEGER PRIMARY KEY, kind TEXT, name TEXT, brief TEXT, meta TEXT);
            CREATE UNIQUE INDEX ux_entry ON entry(kind, name);
            CREATE TABLE term(word TEXT, entry_id INTEGER, PRIMARY KEY(word, entry_id)) WITHOUT ROWID;
            CREATE TABLE news(id TEXT PRIMARY KEY, title TEXT, kind TEXT, published_at TEXT,
                              url TEXT, body TEXT, body_hash TEXT, fetched_at TEXT);
            """
        )
        con.commit()
        con.close()

        html_body = "<p>本周<b>维护</b>公告</p><script>x=1</script><p>10月1日 7:00-9:00</p>"
        assert clean(html_body) == "本周维护公告\n10月1日 7:00-9:00", clean(html_body)
        assert to_time({"inputtime": "1700000000"}, "").startswith("2023-11")
        assert to_time({}, "2026/11/24") == "2026-11-24"

        calls = []
        real = fetch

        def fake(proxy="", limit=LIMIT, api=API):
            calls.append(proxy)
            return [
                {
                    "title": "例行维护公告",
                    "type": "官方公告",
                    "date": "2026/11/24",
                    "url": "https://jx3.xoyo.com/announce/gg.html?id=1",
                    "desc": {"id": 1, "content": "<p>十点维护</p>"},
                }
            ]

        globals()["fetch"] = fake
        try:
            assert sync(db) == (1, 0)  # 首次新增
            assert sync(db, proxy="http://127.0.0.1:7890") == (0, 0)  # 没变
            assert calls[-1] == "http://127.0.0.1:7890"  # 代理确实传下去了
            cache = {"v": "<p>十点维护，预计两小时</p>"}
            globals()["fetch"] = lambda proxy="", limit=LIMIT, api=API: [
                {
                    "title": "例行维护公告",
                    "type": "官方公告",
                    "date": "2026/11/24",
                    "url": "https://jx3.xoyo.com/announce/gg.html?id=1",
                    "desc": {"id": 1, "content": cache["v"]},
                }
            ]
            assert sync(db) == (0, 1)  # 正文变了算更新
        finally:
            globals()["fetch"] = real

        con = sqlite3.connect(db)
        try:
            assert con.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM entry WHERE kind='news'").fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM term WHERE word='例行维护公告'").fetchone()[0] == 1
        finally:
            con.close()
        print("sync self-check ok")


if __name__ == "__main__":
    _self_check()
