"""Bring the local cache in line with Gmail (inbox + archive; not spam/trash)."""

from .gmail import Gmail, parse_message
from .store import Store


def sync(store: Store, gmail: Gmail, query: str = "", log=print) -> tuple[int, int]:
    """(added, removed). Only new messages are fetched, so re-runs are quick
    and an interrupted first run continues where it stopped."""
    me = gmail.profile().get("emailAddress", "").lower()
    if me and me not in store.senders():
        store.set_sender(me, "Other", "self")  # your own sent mail isn't sorted
    log("listing messages...")
    remote = list(gmail.list_ids(query))
    known = store.known_ids()
    new = [i for i in remote if i not in known]
    gone = known - set(remote) if not query else set()
    if gone:
        store.remove_messages(gone)
    log(f"{len(remote)} in Gmail, {len(new)} new, {len(gone)} gone")
    done = 0

    def save(batch):
        nonlocal done
        store.add_messages([parse_message(m) for m in batch])
        done += len(batch)
        if done % 1000 < len(batch) or done == len(new):
            log(f"fetched {done}/{len(new)}")

    gmail.get_metadata(new, on_batch=save)
    store.fill_sender_categories()
    return len(new), len(gone)
