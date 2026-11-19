"""Where the space goes, and picking mail to trash."""

import time

from .store import Store


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def stats(store: Store, top: int = 20) -> str:
    db = store.db
    total_n, total_size = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM messages").fetchone()
    lines = [f"{total_n} mails, {human(total_size)}", "", "By category:"]
    for r in db.execute("""SELECT COALESCE(category, '(unsorted)') c, COUNT(*) n, SUM(size) s
                           FROM messages GROUP BY c ORDER BY s DESC"""):
        lines.append(f"  {r['c']:<15} {r['n']:>7}  {human(r['s']):>9}")
    lines += ["", f"Top {top} senders by size:"]
    for r in db.execute("""SELECT from_addr, COUNT(*) n, SUM(size) s, MAX(category) c FROM messages
                           GROUP BY from_addr ORDER BY s DESC LIMIT ?""", (top,)):
        lines.append(f"  {human(r['s']):>9} {r['n']:>6}  {r['from_addr']}  [{r['c'] or '-'}]")
    lines += ["", f"Top {top} senders by count:"]
    for r in db.execute("""SELECT from_addr, COUNT(*) n, SUM(size) s, MAX(category) c FROM messages
                           GROUP BY from_addr ORDER BY n DESC LIMIT ?""", (top,)):
        lines.append(f"  {r['n']:>6} {human(r['s']):>9}  {r['from_addr']}  [{r['c'] or '-'}]")
    lines += ["", f"Biggest {top} mails:"]
    for r in db.execute("SELECT * FROM messages ORDER BY size DESC LIMIT ?", (top,)):
        day = time.strftime("%Y-%m-%d", time.localtime(r["date"]))
        lines.append(f"  {human(r['size']):>9} {day}  {r['from_addr']}: {r['subject'][:60]}")
    return "\n".join(lines)


def select(store: Store, category: str | None = None, sender: str | None = None,
           older_than_days: int | None = None, min_size_kb: int | None = None,
           keep_starred: bool = True) -> list:
    """Messages matching all the given conditions (for trashing)."""
    where, params = ["1"], []
    if category:
        where.append("category = ?")
        params.append(category)
    if sender:
        where.append("(from_addr = ? OR domain = ?)")
        params += [sender.lower(), sender.lower()]
    if older_than_days is not None:
        where.append("date < ?")
        params.append(int(time.time()) - older_than_days * 86400)
    if min_size_kb is not None:
        where.append("size >= ?")
        params.append(min_size_kb * 1024)
    if keep_starred:
        where.append("',' || labels || ',' NOT LIKE '%,STARRED,%'")
        where.append("',' || labels || ',' NOT LIKE '%,IMPORTANT,%' OR category IN ('Promotions','Social','Notifications','Newsletters')")
    if len(where) == (3 if keep_starred else 1):
        raise ValueError("give at least one condition (category, sender, age or size)")
    return store.messages(" AND ".join(f"({w})" for w in where), params)
