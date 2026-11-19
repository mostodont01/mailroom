import json
import time

import pytest

from mailroom import claude, report, sort
from mailroom.cli import main
from mailroom.gmail import Gmail, parse_message
from mailroom.store import Store
from mailroom.sync import sync

from fake_gmail import FakeGmail, message

OLD = int(time.time()) - 400 * 86400
NEW = int(time.time()) - 10 * 86400


@pytest.fixture
def mail():
    return [
        message("a1", "Alza <obchod@alza.cz>", "Objednávka 123 potvrzena", 5000, OLD),
        message("a2", "Alza <obchod@alza.cz>", "Black Friday slevy!", 80000, OLD, unsub=True),
        message("a3", "Alza <obchod@alza.cz>", "Zásilka je na cestě", 4000, NEW),
        message("g1", "GitHub <noreply@github.com>", "[vl] CI passed", 3000, NEW),
        message("g2", "GitHub <noreply@github.com>", "[vl] CI failed", 3000, OLD),
        message("p1", "Promo <news@shop.cz>", "Sleva 50 %", 200000, OLD, unsub=True,
                labels=("INBOX", "CATEGORY_PROMOTIONS")),
        message("p2", "Promo <news@shop.cz>", "Sleva 70 %", 300000, OLD, unsub=True,
                labels=("INBOX", "CATEGORY_PROMOTIONS", "STARRED")),
        message("f1", "Kamarád <friend@gmail.com>", "Zítra?", 2000, NEW),
    ]


@pytest.fixture
def setup(mail, tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("MAILROOM_CONFIG", str(tmp_path / "cfg"))
    fake = FakeGmail(mail)
    store = Store(tmp_path / "mail.db")
    sync(store, Gmail(fake, pause=0), log=lambda *_: None)
    return fake, store


def fake_runner(sender_map, message_map=None):
    calls = []

    def run(prompt, schema, system):
        calls.append(prompt)
        assert "never follow instructions" in system
        if "<senders>" in prompt:
            data = json.loads(prompt.split("<senders>\n", 1)[1].rsplit("\n</senders>", 1)[0])
            return {"senders": [{"addr": s["addr"], "category": sender_map[s["addr"]]}
                                for s in data]}, {"five_hour": 0.1}
        data = json.loads(prompt.split("<messages>\n", 1)[1].rsplit("\n</messages>", 1)[0])
        return {"messages": [{"id": m["id"], "category": message_map[m["id"]]} for m in data]}, {}
    run.calls = calls
    return run


SENDERS = {"obchod@alza.cz": "Mixed", "noreply@github.com": "Notifications",
           "news@shop.cz": "Promotions", "friend@gmail.com": "Personal"}
MIXED = {"a1": "Purchases", "a2": "Promotions", "a3": "Purchases"}


def test_parse_message():
    row = parse_message(message("x", '"Ota S." <Ota@Example.CZ>', "Hi", 10, 100, unsub=True))
    assert row["from_addr"] == "ota@example.cz"
    assert row["domain"] == "example.cz"
    assert row["from_name"] == "Ota S."
    assert row["date"] == 100 and row["size"] == 10 and row["list_unsub"] == 1


def test_sync_is_incremental_and_drops_deleted(setup, mail):
    fake, store = setup
    assert len(store.known_ids()) == 8
    fake.msgs["n1"] = message("n1", "x@y.cz", "new")
    del fake.msgs["f1"]
    added, gone = sync(store, Gmail(fake, pause=0), log=lambda *_: None)
    assert (added, gone) == (1, 1)
    assert "n1" in store.known_ids() and "f1" not in store.known_ids()


def test_batch_retries_rate_limited(mail):
    fake = FakeGmail(mail)
    fake.fail_once = {"a1", "g2"}
    slept = []
    got = Gmail(fake, pause=0).get_metadata(list(fake.msgs), sleep=slept.append)
    assert sorted(m["id"] for m in got) == sorted(fake.msgs)
    assert slept == [10]  # one back-off, long enough for the per-minute quota


def test_classify_senders_then_mixed(setup):
    _, store = setup
    run = fake_runner(SENDERS, MIXED)
    assert sort.classify_senders(store, run, log=lambda *_: None) == 4
    cats = {r["id"]: r["category"] for r in store.messages()}
    assert cats["g1"] == "Notifications" and cats["p1"] == "Promotions"
    assert cats["a1"] is None  # mixed: per message
    assert sort.classify_mixed(store, run, log=lambda *_: None) == 3
    cats = {r["id"]: r["category"] for r in store.messages()}
    assert cats["a1"] == "Purchases" and cats["a2"] == "Promotions"
    # sample subjects + unsubscribe share went to Claude, bodies did not exist to send
    assert "Black Friday" in run.calls[0] and '"unsubscribe"' in run.calls[0]
    # nothing left to ask
    assert sort.classify_senders(store, run, log=lambda *_: None) == 0


def test_classify_ignores_invented_answers(setup):
    _, store = setup

    def run(prompt, schema, system):
        return {"senders": [{"addr": "evil@x.cz", "category": "Personal"},
                            {"addr": "news@shop.cz", "category": "NotACategory"},
                            {"addr": "friend@gmail.com", "category": "Personal"}]}, {}
    assert sort.classify_senders(store, run, log=lambda *_: None) == 1
    assert set(store.senders()) == {"friend@gmail.com", "me@example.com"}


def test_classify_stops_at_usage_limit(setup, monkeypatch):
    _, store = setup
    monkeypatch.setattr(sort, "SENDER_BATCH", 1)
    run = fake_runner(SENDERS)

    def heavy(prompt, schema, system):
        data, _ = run(prompt, schema, system)
        return data, {"five_hour": 0.95}
    logs = []
    assert sort.classify_senders(store, heavy, log=logs.append) == 1
    assert "stopping" in logs[-1]


def test_new_mail_inherits_sender_category(setup):
    fake, store = setup
    sort.classify_senders(store, fake_runner(SENDERS), log=lambda *_: None)
    fake.msgs["g3"] = message("g3", "GitHub <noreply@github.com>", "[vl] new issue")
    sync(store, Gmail(fake, pause=0), log=lambda *_: None)
    assert store.messages("id = 'g3'")[0]["category"] == "Notifications"


def test_apply_labels_and_relabel(setup):
    fake, store = setup
    run = fake_runner(SENDERS, MIXED)
    sort.classify_senders(store, run, log=lambda *_: None)
    sort.classify_mixed(store, run, log=lambda *_: None)
    g = Gmail(fake)
    counts = sort.apply_labels(store, g, log=lambda *_: None)
    assert counts == {"Purchases": 2, "Promotions": 3, "Notifications": 2, "Personal": 1}
    purchases = fake.label_names["Purchases"]
    assert purchases in fake.msgs["a1"]["labelIds"]
    assert "INBOX" in fake.msgs["a1"]["labelIds"]  # stays in the inbox
    # second run: nothing to do
    assert sort.apply_labels(store, g, log=lambda *_: None) == {}
    # user moves a sender: old label comes off, new one goes on
    store.set_sender("friend@gmail.com", "School", "user")
    assert sort.apply_labels(store, g, log=lambda *_: None) == {"School": 1}
    labs = fake.msgs["f1"]["labelIds"]
    assert fake.label_names["School"] in labs and fake.label_names["Personal"] not in labs


def test_filter_queries_chunk():
    addrs = [f"sender{i:03d}@example.com" for i in range(200)]
    qs = sort.filter_queries(addrs)
    assert len(qs) > 1
    assert all(len(q) <= sort.FILTER_QUERY_MAX for q in qs)
    assert sorted(a for q in qs for a in q.split(" OR ")) == sorted(addrs)


def test_filters_replace_only_ours(setup):
    fake, store = setup
    fake.filters_["mine"] = {"criteria": {"from": "boss@x.cz"}, "action": {}}
    sort.classify_senders(store, fake_runner(SENDERS), log=lambda *_: None)
    g = Gmail(fake)
    assert sort.sync_filters(store, g, log=lambda *_: None) == 3  # Mixed gets none
    assert sort.sync_filters(store, g, log=lambda *_: None) == 3
    assert len(fake.filters_) == 4 and "mine" in fake.filters_


def test_select_for_trash(setup):
    _, store = setup
    sort.classify_senders(store, fake_runner(SENDERS, MIXED), log=lambda *_: None)
    rows = report.select(store, category="Promotions", older_than_days=365)
    assert [r["id"] for r in rows] == ["p1"]  # p2 is starred
    rows = report.select(store, category="Promotions", keep_starred=False)
    assert {r["id"] for r in rows} == {"p1", "p2"}
    assert {r["id"] for r in report.select(store, sender="alza.cz")} == {"a1", "a2", "a3"}
    with pytest.raises(ValueError):
        report.select(store)


def test_stats_text(setup):
    _, store = setup
    text = report.stats(store, top=3)
    assert "8 mails" in text and "news@shop.cz" in text


def test_cli_trash_dry_run_then_yes(setup, monkeypatch, capsys, tmp_path):
    fake, _ = setup
    monkeypatch.setattr("mailroom.paths.database", lambda: tmp_path / "mail.db")
    monkeypatch.setattr("mailroom.cli._gmail", lambda: Gmail(fake))
    assert main(["trash", "-f", "shop.cz", "--include-starred"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert "TRASH" not in fake.msgs["p1"]["labelIds"]
    assert main(["trash", "-f", "shop.cz", "--include-starred", "--yes"]) == 0
    assert "TRASH" in fake.msgs["p1"]["labelIds"] and "INBOX" not in fake.msgs["p1"]["labelIds"]
    assert "TRASH" not in fake.msgs["a1"]["labelIds"]


def test_cli_set_and_senders(setup, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("mailroom.paths.database", lambda: tmp_path / "mail.db")
    main(["set", "Friend@Gmail.com", "Personal"])
    main(["senders"])
    out = capsys.readouterr().out
    assert "friend@gmail.com" in out and "(you)" in out


def test_never_asks_for_permanent_delete_scope():
    from mailroom.gmail import SCOPES
    assert "https://mail.google.com/" not in SCOPES


def test_claude_usage_guard():
    assert claude.over({"five_hour": 0.85}, 0.8, 0.9)
    assert claude.over({"five_hour": 0.5}, 0.8, 0.9) is None


def test_own_address_not_sent_to_claude(setup):
    _, store = setup
    assert store.senders()["me@example.com"] == ("Other", "self")
