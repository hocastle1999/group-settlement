"""REST API 테스트 (MySQL 테스트 DB 필요)."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.db import store
from app.db.tables import settlement_groups

# ---------------------------------------------------------------- 도우미


def create_group(client, name="제주 여행", currency="KRW", members=("민수", "지영", "현우")):
    res = client.post("/api/groups", json={"name": name, "currency": currency})
    assert res.status_code == 201, res.text
    body = res.json()
    headers = {"Authorization": f"Bearer {body['invite_token']}"}
    ids = {}
    for member in members:
        r = client.post(f"/api/groups/{body['id']}/members", json={"name": member}, headers=headers)
        assert r.status_code == 201, r.text
        ids[member] = r.json()["id"]
    return body["id"], headers, ids


def write(client, path, headers, json=None, key=None):
    """멱등성 키를 붙여 쓰기 요청을 보낸다 (키를 안 주면 새 키)."""
    h = {**headers, "Idempotency-Key": key or str(uuid.uuid4())}
    return client.post(path, json=json, headers=h)


def expense(ids, payer="민수", amount="30000", split=None, memo=""):
    split = split or {"type": "equal", "member_ids": list(ids.values())}
    return {"payer_id": ids[payer], "amount": amount, "split": split, "memo": memo}


def balances(client, gid, headers):
    res = client.get(f"/api/groups/{gid}/balances", headers=headers)
    assert res.status_code == 200, res.text
    return {b["name"]: b["amount"] for b in res.json()["balances"]}


# ---------------------------------------------------------------- 전체 흐름


def leftover_taker(ids: dict[str, int], seq: int, count: int = 1) -> set[str]:
    """균등 분할에서 남는 단위를 받는 멤버 이름.

    규칙(app/ledger/split.py): 멤버 ID를 "문자열로" 정렬한 순서에서 분개 순번(seq)번째부터 차례로.
    id 자릿수가 바뀌면("99" -> "100") 문자열 순서가 생성 순서와 달라지므로
    이름을 하드코딩하지 않는다.
    """
    order = sorted(ids, key=lambda name: str(ids[name]))
    return {order[(seq + k) % len(order)] for k in range(count)}


def test_full_flow_expense_settle_transfer_reverse(client):
    gid, headers, ids = create_group(client)

    res = write(client, f"/api/groups/{gid}/expenses", headers, expense(ids, amount="10000"))
    assert res.status_code == 201, res.text
    first = res.json()
    assert (first["kind"], first["seq"]) == ("EXPENSE", 1)
    # 10,000원 / 3명 = 3,334 + 3,333 + 3,333. 남는 1원은 순번 1만큼 돌린 출발점의 멤버
    share = {name: 3333 for name in ids}
    (extra,) = leftover_taker(ids, seq=1)
    share[extra] = 3334
    debit = {ln["member_id"]: ln["amount"] for ln in first["lines"] if ln["side"] == "DEBIT"}
    assert debit == {ids[name]: str(v) for name, v in share.items()}
    expected = {name: -v for name, v in share.items()}
    expected["민수"] += 10_000
    assert balances(client, gid, headers) == {k: str(v) for k, v in expected.items()}

    res = client.get(f"/api/groups/{gid}/settlement", headers=headers)
    plan = res.json()
    assert plan["strategy"] == "auto"
    # 지영과 현우가 각자 몫을 민수에게: 송금 2번
    assert sorted(
        (t["sender_id"], t["receiver_id"], t["amount"]) for t in plan["transfers"]
    ) == sorted((ids[name], ids["민수"], str(share[name])) for name in ("지영", "현우"))

    # 정산 계획대로 송금하면 모두 0
    for t in plan["transfers"]:
        body = {"sender_id": t["sender_id"], "receiver_id": t["receiver_id"], "amount": t["amount"]}
        assert write(client, f"/api/groups/{gid}/transfers", headers, body).status_code == 201
    assert set(balances(client, gid, headers).values()) == {"0"}
    assert client.get(f"/api/groups/{gid}/settlement", headers=headers).json()["transfers"] == []

    # 첫 지출을 취소하면 송금만 남는다 (지출 효과의 정반대)
    res = write(client, f"/api/groups/{gid}/entries/{first['id']}/reversal", headers, {"memo": "x"})
    assert res.status_code == 201, res.text
    assert res.json()["reverses_entry_id"] == first["id"]
    assert balances(client, gid, headers) == {k: str(-v) for k, v in expected.items()}


def test_leftover_unit_rotates_between_members(client):
    """10,000원을 3명이 나누는 지출 3번: 남는 1원을 세 사람이 한 번씩 낸다."""
    gid, headers, ids = create_group(client)
    paid = {member_id: 0 for member_id in ids.values()}
    for _ in range(3):
        entry = write(client, f"/api/groups/{gid}/expenses", headers, expense(ids, amount="10000"))
        for ln in entry.json()["lines"]:
            if ln["side"] == "DEBIT":
                paid[ln["member_id"]] += int(ln["amount"])
    assert set(paid.values()) == {10_000}  # 3,334 + 3,333 + 3,333씩


def test_group_info_and_token_is_shown_only_at_creation(client, db):
    gid, headers, ids = create_group(client)
    res = client.get(f"/api/groups/{gid}", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert "invite_token" not in body
    assert [x["name"] for x in body["members"]] == ["민수", "지영", "현우"]

    # DB에는 토큰 원문이 없고 32바이트 해시만 있다
    token = headers["Authorization"].removeprefix("Bearer ")
    with db.connect() as conn:
        stored = conn.execute(select(settlement_groups.c.invite_token_hash)).scalar_one()
    assert len(stored) == 32 and token.encode() not in stored


def test_ratio_and_fixed_splits(client):
    gid, headers, ids = create_group(client)
    ratio = {
        "type": "ratio",
        "weights": [
            {"member_id": ids["민수"], "weight": 2},
            {"member_id": ids["지영"], "weight": 1},
        ],
    }
    res = write(client, f"/api/groups/{gid}/expenses", headers, expense(ids, "현우", "900", ratio))
    assert res.status_code == 201, res.text
    fixed = {
        "type": "fixed",
        "amounts": [
            {"member_id": ids["민수"], "amount": "100"},
            {"member_id": ids["현우"], "amount": "200"},
        ],
    }
    res = write(client, f"/api/groups/{gid}/expenses", headers, expense(ids, "지영", "300", fixed))
    assert res.status_code == 201, res.text
    # 민수 -600 -100, 지영 -300 +300, 현우 +900 -200
    assert balances(client, gid, headers) == {"민수": "-700", "지영": "0", "현우": "700"}


def test_usd_keeps_cents(client):
    gid, headers, ids = create_group(client, currency="USD", members=("a", "b", "c"))
    res = write(client, f"/api/groups/{gid}/expenses", headers, expense(ids, "a", "10.00"))
    assert res.status_code == 201, res.text
    # 10.00 / 3 = 3.34 + 3.33 + 3.33 (남는 1센트는 순번 1만큼 돌린 출발점의 멤버)
    (extra,) = leftover_taker(ids, seq=1)
    share = {name: Decimal("3.34") if name == extra else Decimal("3.33") for name in ids}
    expected = {name: -v for name, v in share.items()}
    expected["a"] += Decimal("10.00")
    assert balances(client, gid, headers) == {k: str(v) for k, v in expected.items()}


def test_entries_are_listed_newest_first_with_cursor(client):
    gid, headers, ids = create_group(client)
    created = []
    for i in range(5):
        body = {"sender_id": ids["지영"], "receiver_id": ids["민수"], "amount": str(100 + i)}
        created.append(write(client, f"/api/groups/{gid}/transfers", headers, body).json()["id"])

    page1 = client.get(f"/api/groups/{gid}/entries?limit=2", headers=headers).json()
    assert [e["id"] for e in page1["entries"]] == created[::-1][:2]
    assert [e["seq"] for e in page1["entries"]] == [5, 4]  # 그룹 안 순번
    cursor = page1["next_before_id"]
    page2 = client.get(f"/api/groups/{gid}/entries?limit=2&before_id={cursor}", headers=headers)
    page3 = client.get(
        f"/api/groups/{gid}/entries?limit=2&before_id={page2.json()['next_before_id']}",
        headers=headers,
    ).json()
    assert [e["id"] for e in page2.json()["entries"]] == created[::-1][2:4]
    assert [e["id"] for e in page3["entries"]] == created[:1]
    assert page3["next_before_id"] is None


# ---------------------------------------------------------------- 인증 (초대 토큰)


def test_json_responses_declare_utf8(client):
    """PowerShell 5.1은 charset이 없으면 한글 응답을 ISO-8859-1로 읽는다. 오류 응답도 포함."""
    gid, headers, _ = create_group(client)
    ok = client.get(f"/api/groups/{gid}", headers=headers)
    unauthorized = client.get(f"/api/groups/{gid}")
    invalid = client.post("/api/groups", json={"name": "x", "currency": "BTC"})
    for res in (ok, unauthorized, invalid):
        assert res.headers["content-type"] == "application/json; charset=utf-8"
    assert ok.json()["members"][0]["name"] == "민수"


def test_missing_token_is_401(client):
    gid, _, _ = create_group(client)
    res = client.get(f"/api/groups/{gid}/balances")
    assert res.status_code == 401
    assert res.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_token_and_missing_group_look_the_same(client):
    """토큰이 틀려도, 그룹이 없어도 같은 404. 그룹이 있는지 알 수 없어야 한다."""
    gid, headers, _ = create_group(client)
    other_gid, other_headers, _ = create_group(client, name="다른 모임")
    wrong = {"Authorization": "Bearer not-the-token"}

    responses = [
        client.get(f"/api/groups/{gid}/balances", headers=wrong),  # 있는 그룹, 틀린 토큰
        client.get("/api/groups/1000000000000/balances", headers=wrong),  # 없는 그룹
        client.get(f"/api/groups/{gid}/balances", headers=other_headers),  # 다른 그룹의 토큰
    ]
    assert {r.status_code for r in responses} == {404}
    assert len({r.text for r in responses}) == 1
    assert client.get(f"/api/groups/{other_gid}/balances", headers=other_headers).status_code == 200


# ---------------------------------------------------------------- 멱등성


def test_retry_with_same_key_returns_same_entry(client):
    gid, headers, ids = create_group(client)
    path = f"/api/groups/{gid}/expenses"
    first = write(client, path, headers, expense(ids), key="retry-1")
    again = write(client, path, headers, expense(ids), key="retry-1")
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    entries = client.get(f"/api/groups/{gid}/entries", headers=headers).json()["entries"]
    assert len(entries) == 1
    assert balances(client, gid, headers)["민수"] == "20000"  # 두 번 기록됐다면 40000


def test_same_key_with_different_body_is_422(client):
    gid, headers, ids = create_group(client)
    path = f"/api/groups/{gid}/expenses"
    assert write(client, path, headers, expense(ids, amount="100"), key="k1").status_code == 201
    res = write(client, path, headers, expense(ids, amount="200"), key="k1")
    assert res.status_code == 422
    assert res.json()["error"] == "IdempotencyKeyReusedError"
    # 같은 키를 다른 작업(송금)에 써도 다른 요청이다
    body = {"sender_id": ids["지영"], "receiver_id": ids["민수"], "amount": "100"}
    assert write(client, f"/api/groups/{gid}/transfers", headers, body, key="k1").status_code == 422


def test_field_order_does_not_change_request_identity(client):
    gid, headers, ids = create_group(client)
    path = f"/api/groups/{gid}/transfers"
    a = {"sender_id": ids["지영"], "receiver_id": ids["민수"], "amount": "100"}
    b = {"amount": "100", "receiver_id": ids["민수"], "sender_id": ids["지영"]}
    assert write(client, path, headers, a, key="order").status_code == 201
    assert write(client, path, headers, b, key="order").headers["Idempotent-Replayed"] == "true"


@pytest.mark.parametrize("key", [None, "", "has space", "x" * 65, "a.b"])
def test_write_requires_valid_idempotency_key(client, key):
    gid, headers, ids = create_group(client)
    h = dict(headers)
    if key is not None:
        h["Idempotency-Key"] = key
    res = client.post(f"/api/groups/{gid}/expenses", json=expense(ids), headers=h)
    assert res.status_code == 400


def test_reversal_retry_is_idempotent_but_second_reversal_conflicts(client):
    gid, headers, ids = create_group(client)
    entry = write(client, f"/api/groups/{gid}/expenses", headers, expense(ids)).json()
    path = f"/api/groups/{gid}/entries/{entry['id']}/reversal"
    first = write(client, path, headers, key="rev")
    assert first.status_code == 201
    assert write(client, path, headers, key="rev").json() == first.json()  # 재시도
    res = write(client, path, headers)  # 새 키로 다시 취소
    assert res.status_code == 409
    assert res.json()["error"] == "AlreadyReversedError"


# ---------------------------------------------------------------- 오류 매핑


@pytest.mark.parametrize(
    ("change", "status", "error"),
    [
        ({"amount": 30000}, 422, None),  # JSON 숫자 금액은 거부 (float 오차)
        ({"amount": "1.5"}, 422, "InvalidAmountError"),  # 원은 소수 없음
        ({"amount": "0"}, 422, "InvalidAmountError"),
        ({"payer_id": 10**15}, 422, "UnknownMemberError"),
        ({"split": {"type": "equal", "member_ids": []}}, 422, None),
        ({"split": {"type": "unknown"}}, 422, None),
        ({"unexpected": 1}, 422, None),  # 모르는 필드
        (
            {
                "split": {
                    "type": "fixed",
                    "amounts": [{"member_id": 0, "amount": "1"}],  # 합이 총액과 다름
                }
            },
            422,
            "InvalidSplitError",
        ),
    ],
)
def test_invalid_expense_requests(client, change, status, error):
    gid, headers, ids = create_group(client)
    res = write(client, f"/api/groups/{gid}/expenses", headers, {**expense(ids), **change})
    assert res.status_code == status, res.text
    if error:
        assert res.json()["error"] == error


def test_duplicate_split_member_is_rejected(client):
    gid, headers, ids = create_group(client)
    split = {
        "type": "ratio",
        "weights": [
            {"member_id": ids["민수"], "weight": 1},
            {"member_id": ids["민수"], "weight": 2},
        ],
    }
    res = write(client, f"/api/groups/{gid}/expenses", headers, expense(ids, split=split))
    assert res.status_code == 422
    assert res.json()["error"] == "InvalidSplitError"


def test_transfer_to_self_and_unknown_entry(client):
    gid, headers, ids = create_group(client)
    body = {"sender_id": ids["민수"], "receiver_id": ids["민수"], "amount": "100"}
    assert write(client, f"/api/groups/{gid}/transfers", headers, body).status_code == 422
    res = write(client, f"/api/groups/{gid}/entries/1000000000000/reversal", headers)
    assert res.status_code == 404
    assert res.json()["error"] == "EntryNotFoundError"


def test_duplicate_member_is_409(client):
    gid, headers, _ = create_group(client)
    res = client.post(f"/api/groups/{gid}/members", json={"name": "민수"}, headers=headers)
    assert res.status_code == 409


def test_invalid_group_requests(client):
    assert client.post("/api/groups", json={"name": "x", "currency": "BTC"}).status_code == 422
    assert client.post("/api/groups", json={"name": "  ", "currency": "KRW"}).status_code == 422


def test_unbalanced_ledger_is_500_without_details(client, monkeypatch):
    """원장이 깨진 경우(버그)는 클라이언트 잘못이 아니므로 500. 내부 값은 응답에 넣지 않는다."""
    gid, headers, _ = create_group(client)

    def broken(_engine, _group):
        raise store.UnbalancedBalancesError("잔액 합이 1원")

    monkeypatch.setattr(store, "balances", broken)
    res = client.get(f"/api/groups/{gid}/balances", headers=headers)
    assert res.status_code == 500
    assert "1원" not in res.text


# ---------------------------------------------------------------- append-only


def test_app_never_updates_or_deletes_ledger_rows(client, db):
    """전체 흐름에서 앱이 보내는 SQL을 모두 보고, 원장 테이블에 UPDATE/DELETE가 없는지 확인한다."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().upper())

    event.listen(db, "before_cursor_execute", record)
    try:
        gid, headers, ids = create_group(client)
        entry = write(client, f"/api/groups/{gid}/expenses", headers, expense(ids)).json()
        write(client, f"/api/groups/{gid}/entries/{entry['id']}/reversal", headers)
        body = {"sender_id": ids["지영"], "receiver_id": ids["민수"], "amount": "100"}
        write(client, f"/api/groups/{gid}/transfers", headers, body, key="same")
        write(client, f"/api/groups/{gid}/transfers", headers, body, key="same")
        client.get(f"/api/groups/{gid}/entries", headers=headers)
        client.get(f"/api/groups/{gid}/settlement", headers=headers)
    finally:
        event.remove(db, "before_cursor_execute", record)

    assert any(s.startswith("INSERT INTO JOURNAL_ENTRIES") for s in statements)
    writes = [s for s in statements if s.startswith(("UPDATE", "DELETE"))]
    # 그룹 행의 카운터(원장 버전 last_seq, 감사 로그 기준점 audit_seq/audit_head)만 고친다.
    # 분개, 줄, 멱등성 키, 감사 로그는 INSERT만 한다.
    assert writes
    assert all(s.startswith("UPDATE SETTLEMENT_GROUPS SET") for s in writes), writes
    assert any(s.startswith("INSERT INTO AUDIT_EVENTS") for s in statements)


# ---------------------------------------------------------------- 데모 페이지 (6단계)


def test_demo_page_is_served_with_security_headers(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "모임 정산" in res.text
    csp = res.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src" not in csp  # 인라인 스크립트 금지
    assert res.headers["Referrer-Policy"] == "no-referrer"
    js = client.get("/static/app.js")
    assert js.status_code == 200
    # 사용자 입력은 textContent로만 넣는다: HTML로 해석하는 대입이 없어야 한다
    assert not re.search(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML", js.text)


def test_currencies_endpoint_lists_supported_currencies(client):
    body = client.get("/api/currencies").json()
    by_code = {c["code"]: c["exponent"] for c in body["currencies"]}
    assert by_code["KRW"] == 0 and by_code["USD"] == 2 and by_code["KWD"] == 3
