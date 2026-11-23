"""다중 통화 API 테스트 (MySQL 테스트 DB 필요)."""

from __future__ import annotations

import uuid

import pytest

from app.api.auth import hash_token
from app.db import store

# ---------------------------------------------------------------- 도우미


def setup_group(client, currency="KRW", members=("a", "b", "c")):
    body = client.post("/api/groups", json={"name": "여행", "currency": currency}).json()
    headers = {"Authorization": f"Bearer {body['invite_token']}"}
    ids = {}
    for name in members:
        r = client.post(f"/api/groups/{body['id']}/members", json={"name": name}, headers=headers)
        ids[name] = r.json()["id"]
    return body["id"], headers, ids


def post_expense(client, gid, headers, body, key=None):
    h = {**headers, "Idempotency-Key": key or str(uuid.uuid4())}
    return client.post(f"/api/groups/{gid}/expenses", json=body, headers=h)


def equal(ids):
    return {"type": "equal", "member_ids": list(ids.values())}


def balances(client, gid, headers):
    res = client.get(f"/api/groups/{gid}/balances", headers=headers).json()
    return {b["name"]: b["amount"] for b in res["balances"]}


# ---------------------------------------------------------------- 기록


def test_foreign_expense_is_recorded_in_group_currency_with_its_source(client):
    gid, headers, ids = setup_group(client)
    body = {
        "payer_id": ids["a"],
        "amount": "3000",
        "currency": "JPY",
        "fx_rate": "9.1234",
        "split": equal(ids),
        "memo": "라멘",
    }
    res = post_expense(client, gid, headers, body)
    assert res.status_code == 201, res.text
    entry = res.json()
    assert entry["original"] == {"currency": "JPY", "amount": "3000", "fx_rate": "9.1234"}
    credit = [ln["amount"] for ln in entry["lines"] if ln["side"] == "CREDIT"]
    debit = sorted(int(ln["amount"]) for ln in entry["lines"] if ln["side"] == "DEBIT")
    assert credit == ["27370"]  # 3000 x 9.1234 = 27370.2 -> 27370 (원)
    assert debit == [9123, 9123, 9124]
    total = sum(int(v) for v in balances(client, gid, headers).values())
    assert total == 0

    # 목록에서도 증빙이 보인다
    listed = client.get(f"/api/groups/{gid}/entries", headers=headers).json()["entries"]
    assert listed[0]["original"] == entry["original"]


def test_base_currency_expense_has_no_source(client):
    gid, headers, ids = setup_group(client)
    body = {"payer_id": ids["a"], "amount": "3000", "currency": "KRW", "split": equal(ids)}
    res = post_expense(client, gid, headers, body)
    assert res.status_code == 201, res.text
    assert res.json()["original"] is None


def test_fixed_split_in_foreign_currency(client):
    gid, headers, ids = setup_group(client)
    body = {
        "payer_id": ids["a"],
        "amount": "3000",
        "currency": "JPY",
        "fx_rate": "9.1235",
        "split": {
            "type": "fixed",
            "amounts": [{"member_id": ids[m], "amount": "1000"} for m in ("a", "b", "c")],
        },
    }
    res = post_expense(client, gid, headers, body)
    assert res.status_code == 201, res.text
    debit = sorted(int(ln["amount"]) for ln in res.json()["lines"] if ln["side"] == "DEBIT")
    # 각자 환산하면 9124 x 3 = 27372로 총액 27370과 어긋난다. 총액을 1:1:1로 나눈다.
    assert debit == [9123, 9123, 9124]


def test_three_decimal_currency(client):
    gid, headers, ids = setup_group(client, currency="USD")
    body = {
        "payer_id": ids["a"],
        "amount": "1.234",
        "currency": "KWD",
        "fx_rate": "3.25",
        "split": equal(ids),
    }
    res = post_expense(client, gid, headers, body)
    assert res.status_code == 201, res.text
    entry = res.json()
    assert entry["original"]["amount"] == "1.234"
    assert [ln["amount"] for ln in entry["lines"] if ln["side"] == "CREDIT"] == ["4.01"]


def test_reversing_a_foreign_expense(client):
    gid, headers, ids = setup_group(client)
    body = {
        "payer_id": ids["b"],
        "amount": "12.34",
        "currency": "USD",
        "fx_rate": "1388.77",
        "split": equal(ids),
    }
    entry = post_expense(client, gid, headers, body).json()
    h = {**headers, "Idempotency-Key": str(uuid.uuid4())}
    res = client.post(f"/api/groups/{gid}/entries/{entry['id']}/reversal", json={}, headers=h)
    assert res.status_code == 201, res.text
    assert res.json()["original"] is None  # 증빙은 원래 분개에만
    assert set(balances(client, gid, headers).values()) == {"0"}


def test_retry_returns_same_foreign_entry(client):
    gid, headers, ids = setup_group(client)
    body = {
        "payer_id": ids["a"],
        "amount": "50",
        "currency": "EUR",
        "fx_rate": "1500.123",
        "split": equal(ids),
    }
    first = post_expense(client, gid, headers, body, key="fx-retry")
    again = post_expense(client, gid, headers, body, key="fx-retry")
    assert again.json() == first.json()
    changed = post_expense(client, gid, headers, {**body, "fx_rate": "1500.124"}, key="fx-retry")
    assert changed.status_code == 422  # 같은 키, 다른 환율 = 다른 요청


def test_stored_source_is_checked_when_loaded(client, db):
    """DB에서 되살릴 때 코어가 "환산 금액 = 기록 금액"을 다시 검사한다 (손상된 기록 탐지)."""
    gid, headers, ids = setup_group(client)
    body = {
        "payer_id": ids["a"],
        "amount": "10",
        "currency": "USD",
        "fx_rate": "1400",
        "split": equal(ids),
    }
    entry = post_expense(client, gid, headers, body).json()
    token = headers["Authorization"].removeprefix("Bearer ")
    group = store.find_group(db, gid, hash_token(token))
    with db.connect() as conn:
        posted = store.load_posted(conn, group, entry["id"])
    assert posted.entry.source is not None  # 만들면서 환산 일치 검사를 통과했다
    assert posted.entry.source.converted(group.currency).minor_units == 14000


# ---------------------------------------------------------------- 거부


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"currency": "JPY"}, "InvalidExchangeRateError"),  # 외화인데 환율 없음
        ({"fx_rate": "1.5"}, "InvalidExchangeRateError"),  # 기준 통화인데 환율 있음
        ({"currency": "KRW", "fx_rate": "1"}, "InvalidExchangeRateError"),
        ({"currency": "JPY", "fx_rate": "0"}, "InvalidExchangeRateError"),
        ({"currency": "JPY", "fx_rate": "9.12345678901"}, None),  # 소수 11자리: 형식 오류
        ({"currency": "JPY", "fx_rate": 9.1}, None),  # 숫자(float)로 보낸 환율
        ({"currency": "BTC", "fx_rate": "1"}, None),  # 지원하지 않는 통화
        ({"currency": "USD", "fx_rate": "1400", "amount": "1.005"}, "InvalidAmountError"),
        ({"currency": "USD", "fx_rate": "0.0001", "amount": "0.01"}, "InvalidAmountError"),
    ],
)
def test_invalid_foreign_expenses(client, change, error):
    gid, headers, ids = setup_group(client)
    body = {"payer_id": ids["a"], "amount": "3000", "split": equal(ids), **change}
    res = post_expense(client, gid, headers, body)
    assert res.status_code == 422, res.text
    if error:
        assert res.json()["error"] == error
