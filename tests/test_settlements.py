"""정산 확정 테스트 (MySQL 테스트 DB 필요)."""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from app.db import store
from app.db.store import StaleVersionError, member_key
from app.ledger import EqualSplit, Money, Strategy, expense_entry, get_currency

# ---------------------------------------------------------------- 도우미 (API)


def setup_group(client, members=("민수", "지영", "현우")):
    body = client.post("/api/groups", json={"name": "모임", "currency": "KRW"}).json()
    headers = {"Authorization": f"Bearer {body['invite_token']}"}
    ids = {}
    for name in members:
        r = client.post(f"/api/groups/{body['id']}/members", json={"name": name}, headers=headers)
        ids[name] = r.json()["id"]
    return body["id"], headers, ids


def add_expense(client, gid, headers, ids, payer="민수", amount="30000"):
    body = {
        "payer_id": ids[payer],
        "amount": amount,
        "split": {"type": "equal", "member_ids": list(ids.values())},
    }
    h = {**headers, "Idempotency-Key": str(uuid.uuid4())}
    res = client.post(f"/api/groups/{gid}/expenses", json=body, headers=h)
    assert res.status_code == 201, res.text
    return res.json()


def plan(client, gid, headers, strategy="auto"):
    res = client.get(f"/api/groups/{gid}/settlement?strategy={strategy}", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def confirm(client, gid, headers, version, strategy="auto"):
    return client.post(
        f"/api/groups/{gid}/settlements",
        json={"expected_version": version, "strategy": strategy},
        headers=headers,
    )


# ---------------------------------------------------------------- API


def test_confirm_records_the_plan_of_that_version(client):
    gid, headers, ids = setup_group(client)
    add_expense(client, gid, headers, ids)
    add_expense(client, gid, headers, ids, payer="지영", amount="9000")

    current = plan(client, gid, headers)
    assert current["version"] == 2
    res = confirm(client, gid, headers, 2)
    assert res.status_code == 201, res.text
    confirmed = res.json()
    assert confirmed["version"] == confirmed["current_version"] == 2
    # 보여 준 계획 그대로 기록 (확정 응답에는 줄 번호와 완료 여부가 더 붙는다)
    plain = [
        {k: t[k] for k in ("sender_id", "receiver_id", "amount")} for t in confirmed["transfers"]
    ]
    assert plain == current["transfers"]
    assert [t["line_no"] for t in confirmed["transfers"]] == [1, 2][: len(plain)]
    assert confirmed["completed"] is False

    # 같은 버전으로 다시 확정 = 재시도: 처음 기록을 200으로
    again = confirm(client, gid, headers, 2)
    assert again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["id"] == confirmed["id"]

    listed = client.get(f"/api/groups/{gid}/settlements", headers=headers).json()["settlements"]
    assert [x["id"] for x in listed] == [confirmed["id"]]
    one = client.get(f"/api/groups/{gid}/settlements/{confirmed['id']}", headers=headers).json()
    assert one == confirmed


def test_confirm_with_old_version_is_409_with_current_version(client):
    gid, headers, ids = setup_group(client)
    add_expense(client, gid, headers, ids)
    seen = plan(client, gid, headers)["version"]

    add_expense(client, gid, headers, ids, payer="현우")  # 다른 사람이 지출을 추가
    res = confirm(client, gid, headers, seen)
    assert res.status_code == 409
    body = res.json()
    assert body["error"] == "StaleVersionError"
    assert (body["expected_version"], body["current_version"]) == (1, 2)

    # 새 계획을 받아 다시 확정하면 된다
    assert confirm(client, gid, headers, plan(client, gid, headers)["version"]).status_code == 201


def test_confirmed_settlement_shows_when_ledger_moved_on(client):
    gid, headers, ids = setup_group(client)
    add_expense(client, gid, headers, ids)
    confirmed = confirm(client, gid, headers, 1).json()
    add_expense(client, gid, headers, ids)
    again = client.get(f"/api/groups/{gid}/settlements/{confirmed['id']}", headers=headers).json()
    assert (again["version"], again["current_version"]) == (1, 2)


def test_same_version_with_other_strategy_conflicts(client):
    gid, headers, ids = setup_group(client)
    add_expense(client, gid, headers, ids)
    assert confirm(client, gid, headers, 1, "optimal").status_code == 201
    res = confirm(client, gid, headers, 1, "greedy")
    assert res.status_code == 409
    assert res.json()["error"] == "SettlementConflictError"


def test_empty_ledger_confirms_with_no_transfers(client):
    gid, headers, _ = setup_group(client)
    res = confirm(client, gid, headers, 0)
    assert res.status_code == 201
    assert res.json()["transfers"] == []


def test_settlement_of_another_group_is_404(client):
    gid, headers, ids = setup_group(client)
    add_expense(client, gid, headers, ids)
    sid = confirm(client, gid, headers, 1).json()["id"]
    other_gid, other_headers, _ = setup_group(client)
    res = client.get(f"/api/groups/{other_gid}/settlements/{sid}", headers=other_headers)
    assert res.status_code == 404
    assert res.json()["error"] == "SettlementNotFoundError"


@pytest.mark.parametrize("body", [{"expected_version": -1}, {}, {"expected_version": 1, "x": 1}])
def test_invalid_confirm_requests(client, body):
    gid, headers, _ = setup_group(client)
    res = client.post(f"/api/groups/{gid}/settlements", json=body, headers=headers)
    assert res.status_code == 422


# ---------------------------------------------------------------- 지출과 확정의 경쟁 (저장 계층)


def test_confirm_waits_for_inflight_expense_and_then_is_stale(db):
    """지출이 그룹 잠금을 잡고 기록 중일 때 확정이 오면, 확정은 기다렸다가 409가 된다.

    잠금이 없으면 확정은 지출 커밋 전 스냅숏을 보고 "버전 1이 최신"이라며 성공한다.
    그러면 확정 직후 버전 2가 나타나, "확정 순간의 원장 = 확정한 버전"이 깨진다.
    """
    krw = get_currency("KRW")
    group = store.create_group(db, "모임", krw, store.hash_bytes(b"race"))
    ids = [member_key(store.add_member(db, group.id, n).id) for n in ("a", "b")]

    def entry(_conn, _seq):
        return expense_entry(ids[0], Money.of(1000, krw), EqualSplit(ids))

    store.post_entry(db, group, entry, "first", store.hash_bytes(b"1"))
    seen_version = store.current_version(db, group)

    inside = threading.Event()
    release = threading.Event()

    def slow_entry(conn, seq):
        inside.set()  # 잠금을 잡은 상태
        release.wait(timeout=10)
        return entry(conn, seq)

    writer = threading.Thread(
        target=store.post_entry, args=(db, group, slow_entry, "second", store.hash_bytes(b"2"))
    )
    writer.start()
    assert inside.wait(timeout=10)

    outcome: list = []

    def do_confirm():
        try:
            outcome.append(store.confirm_settlement(db, group, seen_version, Strategy.AUTO))
        except StaleVersionError as exc:
            outcome.append(exc)

    confirmer = threading.Thread(target=do_confirm)
    confirmer.start()
    time.sleep(0.5)
    assert confirmer.is_alive()  # 지출이 끝나기를 기다린다

    release.set()
    writer.join(timeout=10)
    confirmer.join(timeout=10)
    assert isinstance(outcome[0], StaleVersionError)
    assert outcome[0].extra == {"expected_version": 1, "current_version": 2}


# ---------------------------------------------------------------- 최적 정산 인원 한도 설정 (5단계)


def five_people_where_greedy_is_not_optimal(client):
    """잔액 a:-3, b:+2, c:+3, d:+2, e:-4 (천 원 단위). 최적 3번, 그리디 4번 (2단계 예시)."""
    gid, headers, ids = setup_group(client, members=("a", "b", "c", "d", "e"))
    for sender, receiver, amount in [("b", "a", 2), ("c", "e", 3), ("d", "e", 1), ("d", "a", 1)]:
        body = {
            "sender_id": ids[sender],
            "receiver_id": ids[receiver],
            "amount": str(amount * 1000),
        }
        h = {**headers, "Idempotency-Key": str(uuid.uuid4())}
        assert client.post(f"/api/groups/{gid}/transfers", json=body, headers=h).status_code == 201
    return gid, headers


@pytest.fixture
def limited_client(db, db_settings):
    from fastapi.testclient import TestClient

    from app.config import AppSettings
    from app.main import create_app

    app = create_app(db_settings, AppSettings(settle_optimal_limit=4))
    app.state.engine = db
    with TestClient(app) as c:
        yield c


def test_auto_uses_optimal_within_the_limit(client):
    gid, headers = five_people_where_greedy_is_not_optimal(client)
    assert len(plan(client, gid, headers)["transfers"]) == 3


def test_limit_setting_switches_auto_to_greedy(limited_client):
    gid, headers = five_people_where_greedy_is_not_optimal(limited_client)
    assert len(plan(limited_client, gid, headers)["transfers"]) == 4  # 5명 > 한도 4 -> 그리디
    res = limited_client.get(f"/api/groups/{gid}/settlement?strategy=optimal", headers=headers)
    assert res.status_code == 422
    assert res.json()["error"] == "TooManyMembersError"


@pytest.mark.parametrize("value", ["0", "21", "abc"])
def test_invalid_limit_setting_is_rejected_at_startup(monkeypatch, value):
    from pydantic import ValidationError

    from app.config import AppSettings

    monkeypatch.setenv("SETTLE_OPTIMAL_LIMIT", value)
    with pytest.raises(ValidationError):
        AppSettings()


# ---------------------------------------------------------------- 송금 완료 (6단계)


def complete(client, gid, headers, sid, line_no, key=None):
    h = {**headers, "Idempotency-Key": key or str(uuid.uuid4())}
    return client.post(
        f"/api/groups/{gid}/settlements/{sid}/transfers/{line_no}/complete", json={}, headers=h
    )


def confirmed_with_two_transfers(client):
    gid, headers, ids = setup_group(client)
    add_expense(client, gid, headers, ids)  # 민수 30,000원, 셋이 나눔 -> 송금 2줄
    settlement = confirm(client, gid, headers, plan(client, gid, headers)["version"]).json()
    assert len(settlement["transfers"]) == 2
    return gid, headers, ids, settlement


def get_settlement(client, gid, headers, sid):
    return client.get(f"/api/groups/{gid}/settlements/{sid}", headers=headers).json()


def test_completing_every_transfer_settles_the_group(client):
    gid, headers, _, settlement = confirmed_with_two_transfers(client)
    sid = settlement["id"]
    first = complete(client, gid, headers, sid, 1)
    assert first.status_code == 201, first.text
    entry = first.json()
    planned = settlement["transfers"][0]
    assert (entry["kind"], entry["settlement_id"], entry["settlement_line_no"]) == (
        "TRANSFER",
        sid,
        1,
    )
    sender = next(ln for ln in entry["lines"] if ln["side"] == "CREDIT")
    assert (sender["member_id"], sender["amount"]) == (planned["sender_id"], planned["amount"])

    after_one = get_settlement(client, gid, headers, sid)
    assert [t["completed_entry_id"] for t in after_one["transfers"]] == [entry["id"], None]
    assert after_one["completed"] is False

    complete(client, gid, headers, sid, 2)
    done = get_settlement(client, gid, headers, sid)
    assert done["completed"] is True
    balances = client.get(f"/api/groups/{gid}/balances", headers=headers).json()["balances"]
    assert {b["amount"] for b in balances} == {"0"}


def test_transfer_line_cannot_be_completed_twice(client):
    gid, headers, _, settlement = confirmed_with_two_transfers(client)
    sid = settlement["id"]
    assert complete(client, gid, headers, sid, 1, key="c1").status_code == 201
    retry = complete(client, gid, headers, sid, 1, key="c1")  # 같은 키 = 재시도
    assert retry.status_code == 201 and retry.headers["Idempotent-Replayed"] == "true"
    again = complete(client, gid, headers, sid, 1)  # 다른 키 = 두 번째 완료
    assert again.status_code == 409
    assert again.json()["error"] == "TransferAlreadyCompletedError"


def test_reversing_a_completion_reopens_the_line(client):
    gid, headers, _, settlement = confirmed_with_two_transfers(client)
    sid = settlement["id"]
    entry = complete(client, gid, headers, sid, 1).json()
    h = {**headers, "Idempotency-Key": str(uuid.uuid4())}
    res = client.post(f"/api/groups/{gid}/entries/{entry['id']}/reversal", json={}, headers=h)
    assert res.status_code == 201
    assert get_settlement(client, gid, headers, sid)["transfers"][0]["completed_entry_id"] is None
    again = complete(client, gid, headers, sid, 1)  # 취소했으니 다시 완료할 수 있다
    assert again.status_code == 201
    assert (
        get_settlement(client, gid, headers, sid)["transfers"][0]["completed_entry_id"]
        == (again.json()["id"])
    )


@pytest.mark.parametrize("line_no", [0, 3])
def test_unknown_transfer_line_is_404(client, line_no):
    gid, headers, _, settlement = confirmed_with_two_transfers(client)
    res = complete(client, gid, headers, settlement["id"], line_no)
    assert res.status_code == 404
    assert res.json()["error"] == "SettlementNotFoundError"


def test_settlement_without_transfers_is_complete_from_the_start(client):
    gid, headers, _ = setup_group(client)
    assert confirm(client, gid, headers, 0).json()["completed"] is True


def test_concurrent_completions_of_one_line_record_one_transfer(db):
    """다른 키로 같은 줄을 동시에 완료해도 송금은 하나. 그룹 잠금 아래의 검사가 막는다."""
    krw = get_currency("KRW")
    group = store.create_group(db, "모임", krw, store.hash_bytes(b"complete"))
    ids = [member_key(store.add_member(db, group.id, n).id) for n in ("a", "b")]
    entry = expense_entry(ids[0], Money.of(1000, krw), EqualSplit(ids))
    store.post_entry(db, group, lambda _c, _s: entry, "e", store.hash_bytes(b"e"))
    settlement, _ = store.confirm_settlement(db, group, 1, Strategy.AUTO)

    n = 4
    start = threading.Barrier(n)
    outcomes: list[str] = []

    def worker(i: int) -> None:
        start.wait(timeout=10)
        build = store.completion_builder(group, settlement.id, 1, "")
        try:
            store.post_entry(
                db,
                group,
                build,
                f"k{i}",
                store.hash_bytes(f"k{i}".encode()),
                link=(settlement.id, 1),
            )
            outcomes.append("ok")
        except store.TransferAlreadyCompletedError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["conflict"] * (n - 1) + ["ok"]
    assert len(store.get_settlement(db, group, settlement.id).completions) == 1
