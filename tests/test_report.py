"""리포트 SQL 테스트 (MySQL 테스트 DB 필요).

SQL로 낸 숫자를 API의 분개 목록으로 파이썬에서 다시 계산한 값과 비교한다.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal

import pytest
from sqlalchemy import text

# ---------------------------------------------------------------- 도우미


def setup_group(client, members=("a", "b", "c")):
    body = client.post("/api/groups", json={"name": "모임", "currency": "KRW"}).json()
    headers = {"Authorization": f"Bearer {body['invite_token']}"}
    ids = {}
    for name in members:
        r = client.post(f"/api/groups/{body['id']}/members", json={"name": name}, headers=headers)
        ids[name] = r.json()["id"]
    return body["id"], headers, ids


def post(client, path, headers, body):
    h = {**headers, "Idempotency-Key": str(uuid.uuid4())}
    res = client.post(path, json=body, headers=h)
    assert res.status_code == 201, res.text
    return res.json()


def expense(ids, payer, amount, **extra):
    split = {"type": "equal", "member_ids": list(ids.values())}
    return {"payer_id": ids[payer], "amount": amount, "split": split, **extra}


def report(client, gid, headers, tz="+00:00"):
    res = client.get(f"/api/groups/{gid}/report", params={"tz_offset": tz}, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def mixed_history(client):
    """지출(원화, 엔화), 송금, 지출 취소, 정산 확정과 송금 완료까지."""
    gid, headers, ids = setup_group(client)
    base = f"/api/groups/{gid}"
    post(client, f"{base}/expenses", headers, expense(ids, "a", "30000"))
    cancelled = post(client, f"{base}/expenses", headers, expense(ids, "b", "9000"))
    post(client, f"{base}/entries/{cancelled['id']}/reversal", headers, {})
    post(
        client,
        f"{base}/expenses",
        headers,
        expense(ids, "c", "3000", currency="JPY", fx_rate="9.1234"),
    )
    body = {"sender_id": ids["b"], "receiver_id": ids["a"], "amount": "5000"}
    post(client, f"{base}/transfers", headers, body)
    version = client.get(f"{base}/settlement", headers=headers).json()["version"]
    sid = client.post(
        f"{base}/settlements", json={"expected_version": version}, headers=headers
    ).json()["id"]
    post(client, f"{base}/settlements/{sid}/transfers/1/complete", headers, {})
    return gid, headers, ids


def recompute_summary(client, gid, headers):
    """분개 목록으로 멤버별 낸 돈/몫/보낸 송금/받은 송금을 다시 계산한다 (SQL과 독립)."""
    entries = client.get(f"/api/groups/{gid}/entries?limit=100", headers=headers).json()["entries"]
    by_id = {e["id"]: e for e in entries}
    totals: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for e in entries:
        reversal = e["kind"] == "REVERSAL"
        kind = by_id[e["reverses_entry_id"]]["kind"] if reversal else e["kind"]
        for ln in e["lines"]:
            side = ln["side"]
            if reversal:
                side = "CREDIT" if side == "DEBIT" else "DEBIT"
            amount = Decimal(ln["amount"]) * (-1 if reversal else 1)
            bucket = {
                ("EXPENSE", "CREDIT"): "paid",
                ("EXPENSE", "DEBIT"): "share",
                ("TRANSFER", "CREDIT"): "sent",
                ("TRANSFER", "DEBIT"): "received",
            }[(kind, side)]
            totals[ln["member_id"]][bucket] += amount
    return totals


# ---------------------------------------------------------------- 멤버별 요약


def test_member_summary_matches_python_and_ledger_balance(client):
    gid, headers, ids = mixed_history(client)
    r = report(client, gid, headers)
    expected = recompute_summary(client, gid, headers)
    balances = {
        b["member_id"]: b["amount"]
        for b in client.get(f"/api/groups/{gid}/balances", headers=headers).json()["balances"]
    }
    assert r["version"] == 6  # 분개 6개 (정산 확정은 분개가 아니다)
    for m in r["members"]:
        for field in ("paid", "share", "sent", "received"):
            assert Decimal(m[field]) == expected[m["member_id"]][field], (m["name"], field)
        assert m["balance"] == balances[m["member_id"]]  # 요약으로 만든 잔액 = 원장 잔액
    # 지출은 낸 돈의 합 = 몫의 합, 송금은 보낸 합 = 받은 합
    assert sum(Decimal(m["paid"]) for m in r["members"]) == sum(
        Decimal(m["share"]) for m in r["members"]
    )
    assert sum(Decimal(m["sent"]) for m in r["members"]) == sum(
        Decimal(m["received"]) for m in r["members"]
    )


def test_cancelled_expense_does_not_count(client):
    gid, headers, ids = mixed_history(client)
    by_name = {m["name"]: m for m in report(client, gid, headers)["members"]}
    # b가 낸 9,000원은 취소됐다. 역분개를 따로 세면 b의 paid가 9,000으로 보였을 것이다.
    assert by_name["b"]["paid"] == "0"
    assert Decimal(by_name["a"]["paid"]) == 30000


# ---------------------------------------------------------------- 날짜별, 통화별


def test_daily_spending_splits_by_requested_time_zone(client, db):
    gid, headers, ids = setup_group(client)
    base = f"/api/groups/{gid}"
    late = post(client, f"{base}/expenses", headers, expense(ids, "a", "1000"))
    later = post(client, f"{base}/expenses", headers, expense(ids, "b", "2000"))
    other_day = post(client, f"{base}/expenses", headers, expense(ids, "c", "4000"))
    with db.begin() as conn:  # 테스트용으로 기록 시각을 정한다 (UTC)
        for entry_id, at in [
            (late["id"], "2026-11-01 14:00:00"),  # 서울 11-01 23:00
            (later["id"], "2026-11-01 15:30:00"),  # 서울 11-02 00:30
            (other_day["id"], "2026-11-03 01:00:00"),  # 서울 11-03 10:00
        ]:
            conn.execute(
                text("UPDATE journal_entries SET created_at = :at WHERE id = :id"),
                {"at": at, "id": entry_id},
            )

    utc = [(d["day"], d["total"], d["cumulative"]) for d in report(client, gid, headers)["daily"]]
    assert utc == [("2026-11-01", "3000", "3000"), ("2026-11-03", "4000", "7000")]
    seoul = report(client, gid, headers, "+09:00")["daily"]
    assert [(d["day"], d["expenses"], d["cumulative"]) for d in seoul] == [
        ("2026-11-01", 1, "1000"),
        ("2026-11-02", 1, "3000"),
        ("2026-11-03", 1, "7000"),
    ]


def test_spending_by_original_currency(client):
    gid, headers, ids = mixed_history(client)
    rows = {c["currency"]: c for c in report(client, gid, headers)["by_currency"]}
    assert rows["KRW"] == {
        "currency": "KRW",
        "expenses": 1,  # 9,000원 지출은 취소됐다
        "original_total": "30000",
        "converted_total": "30000",
    }
    assert rows["JPY"] == {
        "currency": "JPY",
        "expenses": 1,
        "original_total": "3000",
        "converted_total": "27370",
    }


def test_empty_group_report(client):
    gid, headers, _ = setup_group(client)
    r = report(client, gid, headers)
    assert r["daily"] == [] and r["by_currency"] == []
    assert {m["balance"] for m in r["members"]} == {"0"}


@pytest.mark.parametrize("tz", ["09:00", "+9:00", "+15:00", "Asia/Seoul"])
def test_invalid_time_zone_offset_is_422(client, tz):
    gid, headers, _ = setup_group(client)
    res = client.get(f"/api/groups/{gid}/report", params={"tz_offset": tz}, headers=headers)
    assert res.status_code == 422
