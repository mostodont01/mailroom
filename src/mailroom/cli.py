"""mailroom: sort and clean up a Gmail account.

    mailroom login                     browser login (once; again if the token expires)
    mailroom sync                      fetch new mail metadata into the local cache
    mailroom stats                     where the space goes
    mailroom classify                  Claude picks a category per sender (+ per mail for mixed ones)
    mailroom senders [-c CAT]          decided senders
    mailroom set SENDER CATEGORY       override a sender's category
    mailroom label [--dry-run]         add the category labels in Gmail (mail stays in the inbox)
    mailroom filters [--dry-run]       Gmail filters so new mail is labelled on arrival
    mailroom trash [conditions] [--yes]  move matching mail to the trash (dry run without --yes)
"""

import argparse
import sys
import time

from . import report, sort
from .store import Store


def _gmail():
    from .gmail import Gmail, service
    return Gmail(service())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mailroom", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    s = sub.add_parser("sync")
    s.add_argument("-q", "--query", default="", help="Gmail search to limit the sync (testing)")
    s = sub.add_parser("stats")
    s.add_argument("-n", "--top", type=int, default=20)
    s = sub.add_parser("classify")
    s.add_argument("--limit", type=int, help="only the N biggest unsorted senders")
    s.add_argument("--max-5h", type=float, default=0.8)
    s.add_argument("--max-week", type=float, default=0.9)
    s = sub.add_parser("senders")
    s.add_argument("-c", "--category")
    s = sub.add_parser("set")
    s.add_argument("sender")
    s.add_argument("category", choices=list(sort.CATEGORIES) + [sort.MIXED])
    for name in ("label", "filters"):
        s = sub.add_parser(name)
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--prefix", default="", help="label name prefix, e.g. 'Sorted/'")
    s = sub.add_parser("trash")
    s.add_argument("-c", "--category")
    s.add_argument("-f", "--sender", help="address or domain")
    s.add_argument("--older-than", type=int, metavar="DAYS")
    s.add_argument("--min-size", type=int, metavar="KB")
    s.add_argument("--include-starred", action="store_true",
                   help="also starred / important mail (kept by default)")
    s.add_argument("--yes", action="store_true", help="really move them to the trash")
    a = p.parse_args(argv)

    if a.cmd == "login":
        from .gmail import Gmail, login, service
        login(interactive=True)
        print("logged in as", Gmail(service()).profile()["emailAddress"])
        return 0

    store = Store()
    if a.cmd == "sync":
        from .sync import sync
        added, gone = sync(store, _gmail(), a.query)
        print(f"done: {added} new, {gone} removed")
    elif a.cmd == "stats":
        print(report.stats(store, a.top))
    elif a.cmd == "classify":
        n = sort.classify_senders(store, max_5h=a.max_5h, max_week=a.max_week, limit=a.limit)
        m = sort.classify_mixed(store, max_5h=a.max_5h, max_week=a.max_week)
        print(f"done: {n} senders, {m} mails from mixed senders")
    elif a.cmd == "senders":
        counts = {r[0]: r[1] for r in store.db.execute(
            "SELECT from_addr, COUNT(*) FROM messages GROUP BY from_addr")}
        rows = sorted(store.senders().items(), key=lambda kv: (kv[1][0], -counts.get(kv[0], 0)))
        for addr, (cat, src) in rows:
            if not a.category or cat == a.category:
                print(f"{cat:<14} {counts.get(addr, 0):>6}  {addr}{'  (you)' if src == 'user' else ''}")
    elif a.cmd == "set":
        store.set_sender(a.sender.lower(), a.category, "user")
        print(f"{a.sender} -> {a.category}")
    elif a.cmd == "label":
        counts = sort.apply_labels(store, None if a.dry_run else _gmail(), a.prefix, dry_run=a.dry_run)
        for cat, n in sorted(counts.items()):
            print(f"{cat:<14} {n:>6}{' (dry run)' if a.dry_run else ''}")
    elif a.cmd == "filters":
        n = sort.sync_filters(store, None if a.dry_run else _gmail(), a.prefix, dry_run=a.dry_run)
        print(f"{n} filters{' would be made (dry run)' if a.dry_run else ' in Gmail'}")
    elif a.cmd == "trash":
        try:
            rows = report.select(store, a.category, a.sender, a.older_than, a.min_size,
                                 keep_starred=not a.include_starred)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        size = sum(r["size"] for r in rows)
        print(f"{len(rows)} mails, {report.human(size)}")
        for r in rows[:15]:
            day = time.strftime("%Y-%m-%d", time.localtime(r["date"]))
            print(f"  {day}  {r['from_addr']}: {r['subject'][:70]}")
        if len(rows) > 15:
            print(f"  ... and {len(rows) - 15} more")
        if not a.yes:
            print("dry run: add --yes to move them to the trash (Gmail keeps it 30 days)")
            return 0
        _gmail().trash([r["id"] for r in rows])
        store.remove_messages([r["id"] for r in rows])
        print(f"moved {len(rows)} mails to the trash")
    return 0
