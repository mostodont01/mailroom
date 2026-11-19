"""Local SQLite cache of message metadata + the sorting decisions."""

import sqlite3
from pathlib import Path

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT, from_addr TEXT, from_name TEXT, domain TEXT,
    subject TEXT, snippet TEXT, date INTEGER, size INTEGER,
    labels TEXT, list_unsub INTEGER,
    category TEXT,          -- decided category (sender's, or per message for mixed senders)
    labelled TEXT           -- category whose Gmail label was applied
);
CREATE INDEX IF NOT EXISTS msg_from ON messages(from_addr);
CREATE INDEX IF NOT EXISTS msg_cat ON messages(category);
CREATE TABLE IF NOT EXISTS senders (
    addr TEXT PRIMARY KEY,
    category TEXT NOT NULL,  -- a category, or 'Mixed' (decided per message)
    source TEXT NOT NULL     -- 'claude' | 'user'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

COLUMNS = ["id", "thread_id", "from_addr", "from_name", "domain", "subject", "snippet",
           "date", "size", "labels", "list_unsub"]


class Store:
    def __init__(self, path: Path | None = None):
        path = path or paths.database()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # messages
    def known_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT id FROM messages")}

    def add_messages(self, rows: list[dict]) -> None:
        self.db.executemany(
            f"INSERT OR REPLACE INTO messages ({','.join(COLUMNS)}) "
            f"VALUES ({','.join(':' + c for c in COLUMNS)})", rows)
        self.db.commit()

    def remove_messages(self, ids) -> None:
        self.db.executemany("DELETE FROM messages WHERE id = ?", [(i,) for i in ids])
        self.db.commit()

    def messages(self, where: str = "1", params=()) -> list[sqlite3.Row]:
        return self.db.execute(f"SELECT * FROM messages WHERE {where} ORDER BY date DESC",
                               params).fetchall()

    def set_category(self, ids, category: str) -> None:
        self.db.executemany("UPDATE messages SET category = ? WHERE id = ?",
                            [(category, i) for i in ids])
        self.db.commit()

    def set_labelled(self, ids, category: str | None) -> None:
        self.db.executemany("UPDATE messages SET labelled = ? WHERE id = ?",
                            [(category, i) for i in ids])
        self.db.commit()

    # senders
    def sender_summaries(self, only_unsorted: bool = True) -> list[dict]:
        """One row per sender: count, size, unsubscribe share, sample subjects."""
        cond = "WHERE from_addr NOT IN (SELECT addr FROM senders)" if only_unsorted else ""
        rows = self.db.execute(f"""
            SELECT from_addr, MAX(from_name) AS name, COUNT(*) AS n, SUM(size) AS size,
                   AVG(list_unsub) AS unsub, GROUP_CONCAT(labels, ',') AS labels
            FROM messages {cond} GROUP BY from_addr ORDER BY n DESC""").fetchall()
        out = []
        for r in rows:
            subjects = [s[0] for s in self.db.execute(
                "SELECT DISTINCT subject FROM messages WHERE from_addr = ? "
                "ORDER BY date DESC LIMIT 6", (r["from_addr"],))]
            gcats = sorted({lab for lab in (r["labels"] or "").split(",")
                            if lab.startswith("CATEGORY_") or lab == "SENT"})
            out.append({"addr": r["from_addr"], "name": r["name"] or "", "count": r["n"],
                        "size": r["size"] or 0, "unsubscribe": round(r["unsub"] or 0, 2),
                        "gmail": gcats, "subjects": subjects})
        return out

    def set_sender(self, addr: str, category: str, source: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO senders VALUES (?, ?, ?)", (addr, category, source))
        if category != "Mixed":
            self.db.execute("UPDATE messages SET category = ? WHERE from_addr = ?", (category, addr))
        else:
            self.db.execute("UPDATE messages SET category = NULL WHERE from_addr = ?", (addr,))
        self.db.commit()

    def senders(self) -> dict[str, tuple[str, str]]:
        return {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT * FROM senders")}

    def fill_sender_categories(self) -> int:
        """New mail from already-decided senders inherits the sender's category."""
        cur = self.db.execute("""
            UPDATE messages SET category = (SELECT category FROM senders s WHERE s.addr = messages.from_addr)
            WHERE category IS NULL AND from_addr IN
                  (SELECT addr FROM senders WHERE category != 'Mixed')""")
        self.db.commit()
        return cur.rowcount

    # meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()
