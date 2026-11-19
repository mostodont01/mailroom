"""Thin wrapper around the Gmail API.

Scopes: gmail.modify (read, label, move to trash) and gmail.settings.basic
(filters). Deliberately NOT the full https://mail.google.com/ scope, so
mailroom can never delete mail permanently: trash keeps it for 30 days.
"""

import email.utils
import time
from typing import Callable, Iterable, Iterator

from . import paths

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
HEADERS = ["From", "Subject", "Date", "List-Unsubscribe"]
BATCH = 20          # requests per HTTP batch: 20 x 5 units, well under the per-minute user quota
PACE = 0.6          # s between batches (~10,000 units/min; the limit is 15,000)
MODIFY_CHUNK = 1000  # ids per batchModify call (API maximum)


class SetupError(Exception):
    pass


def login(interactive: bool = True):
    """Credentials from the saved token, or a browser login."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if paths.token().exists():
        creds = Credentials.from_authorized_user_file(str(paths.token()), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            return creds
        except RefreshError:
            creds = None  # testing-mode tokens die after 7 days: log in again
    if not interactive:
        raise SetupError("not logged in: run `mailroom login`")
    if not paths.client_secret().exists():
        raise SetupError(f"missing {paths.client_secret()} (see README: Google Cloud setup)")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(paths.client_secret()), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save(creds)
    return creds


def _save(creds) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.token().write_text(creds.to_json(), encoding="utf-8")
    paths.token().chmod(0o600)


def service(interactive: bool = False):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=login(interactive), cache_discovery=False)


class Gmail:
    """The handful of calls mailroom needs. `svc` is a googleapiclient
    resource (or a fake with the same shape in tests)."""

    def __init__(self, svc, pause: float = PACE):
        self.svc = svc
        self.pause = pause  # between batches, to stay under the per-user quota

    def profile(self) -> dict:
        return self.svc.users().getProfile(userId="me").execute()

    def list_ids(self, query: str = "", include_spam_trash: bool = False) -> Iterator[str]:
        token = None
        while True:
            resp = self.svc.users().messages().list(
                userId="me", q=query, maxResults=500, pageToken=token,
                includeSpamTrash=include_spam_trash,
            ).execute()
            for m in resp.get("messages", []):
                yield m["id"]
            token = resp.get("nextPageToken")
            if not token:
                return

    def get_metadata(self, ids: list[str], on_batch: Callable[[list[dict]], None] | None = None,
                     retries: int = 8, sleep=time.sleep) -> list[dict]:
        """Metadata (headers, size, labels, snippet) for many ids, in HTTP batches.
        Rate-limited ones are retried with backoff."""
        out: list[dict] = []
        for start in range(0, len(ids), BATCH):
            todo = list(ids[start:start + BATCH])
            got: list[dict] = []
            for attempt in range(retries + 1):
                failed: list[str] = []

                def cb(request_id, response, exception, _failed=failed, _got=got):
                    if exception is not None:
                        status = getattr(getattr(exception, "resp", None), "status", None)
                        if status == 404:
                            return  # deleted meanwhile
                        _failed.append(request_id)
                    else:
                        _got.append(response)

                batch = self.svc.new_batch_http_request(callback=cb)
                for mid in todo:
                    batch.add(self.svc.users().messages().get(
                        userId="me", id=mid, format="metadata", metadataHeaders=HEADERS,
                    ), request_id=mid)
                batch.execute()
                if not failed:
                    break
                if attempt == retries:
                    raise RuntimeError(f"{len(failed)} messages kept failing (rate limit?)")
                todo = failed
                # the quota is per minute: back off long enough for it to refill
                sleep(min(10 * 2 ** attempt, 65))
            out.extend(got)
            if on_batch:
                on_batch(got)
            if self.pause:
                sleep(self.pause)
        return out

    def labels(self) -> dict[str, str]:
        """name -> id"""
        resp = self.svc.users().labels().list(userId="me").execute()
        return {lab["name"]: lab["id"] for lab in resp.get("labels", [])}

    def ensure_label(self, name: str, existing: dict[str, str] | None = None) -> str:
        existing = self.labels() if existing is None else existing
        if name in existing:
            return existing[name]
        lab = self.svc.users().labels().create(userId="me", body={
            "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show",
        }).execute()
        existing[name] = lab["id"]
        return lab["id"]

    def modify(self, ids: Iterable[str], add: list[str] = (), remove: list[str] = ()) -> int:
        ids = list(ids)
        for start in range(0, len(ids), MODIFY_CHUNK):
            self.svc.users().messages().batchModify(userId="me", body={
                "ids": ids[start:start + MODIFY_CHUNK],
                "addLabelIds": list(add), "removeLabelIds": list(remove),
            }).execute()
        return len(ids)

    def trash(self, ids: Iterable[str]) -> int:
        """Move to trash (Gmail empties it after 30 days). Uses batchModify
        with the TRASH label, so it needs no extra scope."""
        return self.modify(ids, add=["TRASH"], remove=["INBOX"])

    def filters(self) -> list[dict]:
        resp = self.svc.users().settings().filters().list(userId="me").execute()
        return resp.get("filter", [])

    def create_filter(self, from_query: str, label_id: str) -> dict:
        return self.svc.users().settings().filters().create(userId="me", body={
            "criteria": {"from": from_query},
            "action": {"addLabelIds": [label_id]},
        }).execute()

    def delete_filter(self, filter_id: str) -> None:
        self.svc.users().settings().filters().delete(userId="me", id=filter_id).execute()


def parse_message(m: dict) -> dict:
    """API message (format=metadata) -> flat row for the cache."""
    headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
    name, addr = email.utils.parseaddr(headers.get("from", ""))
    addr = addr.lower()
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    return {
        "id": m["id"],
        "thread_id": m.get("threadId", ""),
        "from_addr": addr,
        "from_name": name,
        "domain": domain,
        "subject": headers.get("subject", ""),
        "snippet": m.get("snippet", ""),
        "date": int(m.get("internalDate", 0)) // 1000,
        "size": int(m.get("sizeEstimate", 0)),
        "labels": ",".join(m.get("labelIds", [])),
        "list_unsub": 1 if headers.get("list-unsubscribe") else 0,
    }
