"""In-memory stand-in for the googleapiclient Gmail resource."""


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class HttpError404(Exception):
    class resp:
        status = 404


class FakeGmail:
    def __init__(self, messages: list[dict]):
        self.msgs = {m["id"]: m for m in messages}
        self.label_names = {"INBOX": "INBOX", "STARRED": "STARRED"}
        self.filters_ = {}
        self.calls = []
        self.fail_once = set()  # ids that fail with a rate limit once

    # resource chain
    def users(self):
        return self

    def messages(self):
        return _Messages(self)

    def labels(self):
        return _Labels(self)

    def settings(self):
        return self

    def filters(self):
        return _Filters(self)

    def getProfile(self, userId):
        return _Req(lambda: {"emailAddress": "me@example.com"})

    def new_batch_http_request(self, callback):
        return _Batch(callback)


class _Batch:
    def __init__(self, cb):
        self.cb, self.items = cb, []

    def add(self, req, request_id):
        self.items.append((req, request_id))

    def execute(self):
        for req, rid in self.items:
            try:
                self.cb(rid, req.execute(), None)
            except Exception as e:  # noqa: BLE001
                self.cb(rid, None, e)


class RateLimited(Exception):
    class resp:
        status = 429


class _Messages:
    def __init__(self, g):
        self.g = g

    def list(self, userId, q="", maxResults=500, pageToken=None, includeSpamTrash=False):
        ids = sorted(i for i, m in self.g.msgs.items() if "TRASH" not in m.get("labelIds", []))
        start = int(pageToken or 0)
        page = ids[start:start + maxResults]
        resp = {"messages": [{"id": i} for i in page]}
        if start + maxResults < len(ids):
            resp["nextPageToken"] = str(start + maxResults)
        return _Req(lambda: resp)

    def get(self, userId, id, format, metadataHeaders):
        def run():
            if id in self.g.fail_once:
                self.g.fail_once.discard(id)
                raise RateLimited()
            if id not in self.g.msgs:
                raise HttpError404()
            return self.g.msgs[id]
        return _Req(run)

    def batchModify(self, userId, body):
        def run():
            self.g.calls.append(("modify", body))
            for i in body["ids"]:
                labs = self.g.msgs[i].setdefault("labelIds", [])
                for lab in body["addLabelIds"]:
                    if lab not in labs:
                        labs.append(lab)
                for lab in body["removeLabelIds"]:
                    if lab in labs:
                        labs.remove(lab)
            return {}
        return _Req(run)


class _Labels:
    def __init__(self, g):
        self.g = g

    def list(self, userId):
        return _Req(lambda: {"labels": [{"name": n, "id": i} for n, i in self.g.label_names.items()]})

    def create(self, userId, body):
        def run():
            lid = f"Label_{len(self.g.label_names)}"
            self.g.label_names[body["name"]] = lid
            return {"id": lid, "name": body["name"]}
        return _Req(run)


class _Filters:
    def __init__(self, g):
        self.g = g

    def list(self, userId):
        return _Req(lambda: {"filter": [dict(v, id=k) for k, v in self.g.filters_.items()]})

    def create(self, userId, body):
        def run():
            fid = f"f{len(self.g.filters_)}_{len(self.g.calls)}"
            self.g.filters_[fid] = body
            self.g.calls.append(("filter", body))
            return {"id": fid}
        return _Req(run)

    def delete(self, userId, id):
        return _Req(lambda: self.g.filters_.pop(id))


def message(mid: str, sender: str, subject: str, size: int = 1000, date_s: int = 1_700_000_000,
            labels=("INBOX",), unsub: bool = False) -> dict:
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    if unsub:
        headers.append({"name": "List-Unsubscribe", "value": "<mailto:x@y>"})
    return {"id": mid, "threadId": mid, "labelIds": list(labels), "snippet": f"snippet {subject}",
            "sizeEstimate": size, "internalDate": str(date_s * 1000), "payload": {"headers": headers}}
