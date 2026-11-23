"""저장 계층 테스트 (MySQL 테스트 DB 필요).

- 코어 규칙이 DB 쓰기에도 그대로 적용되는지
- DB 제약(CHECK, 유니크, 복합 외래 키)이 코어를 거치지 않은 쓰기도 막는지
- SQL로 구한 잔액이 메모리 원장(`Ledger`)과 같은지
- 멱등성 키가 재시도와 동시 요청에서 중복 분개를 막는지
"""

from __future__ import annotations

import random
import re
import threading
import time

import pytest
from sqlalchemy import Engine, func, insert, select, text
from sqlalchemy.exc import DBAPIError

from app.db import store
from app.db.store import DuplicateMemberError, Group, IdempotencyKeyReusedError, member_key
from app.db.tables import entry_lines, idempotency_keys, journal_entries, members
from app.ledger import (
    CURRENCIES,
    AlreadyReversedError,
    EntryNotFoundError,
    EqualSplit,
    FixedSplit,
    InvalidSplitError,
    Ledger,
    Money,
    RatioSplit,
    UnknownMemberError,
    expense_entry,
    get_currency,
    total_balance,
    transfer_entry,
)

KRW = get_currency("KRW")

# MySQL 오류 번호
ER_NO_REFERENCED_ROW = 1452  # 외래 키가 가리키는 행이 없다
ER_CHECK_CONSTRAINT_VIOLATED = 3819


def krw(n: int) -> Money:
    return Money.of(n, "KRW")


def make_group(db: Engine, names=("a", "b", "c"), token: bytes = b"t" * 32) -> tuple[Group, dict]:
    group = store.create_group(db, "여행", KRW, store.hash_bytes(token))
    ids = {name: store.add_member(db, group.id, name).id for name in names}
    return group, ids


_keys = iter(range(1, 1_000_000))


def post(db: Engine, group: Group, entry, key: str | None = None):
    """도우미: 고정된 분개를 새 멱등성 키로 기록한다."""
    key = key or f"k{next(_keys)}"
    return store.post_entry(db, group, lambda _c, _s: entry, key, store.hash_bytes(key.encode()))


def reverse(db: Engine, group: Group, entry_id: int, key: str | None = None):
    key = key or f"r{next(_keys)}"
    build = store.reversal_builder(group, entry_id, "취소")
    return store.post_entry(db, group, build, key, store.hash_bytes(key.encode()))


def count(db: Engine, table) -> int:
    with db.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def m(ids: dict, name: str) -> str:
    return member_key(ids[name])


# ---------------------------------------------------------------- 그룹, 멤버


def test_find_group_needs_matching_id_and_token(db):
    group, _ = make_group(db, token=b"right")
    assert store.find_group(db, group.id, store.hash_bytes(b"right")) == group
    assert store.find_group(db, group.id, store.hash_bytes(b"wrong")) is None
    assert store.find_group(db, group.id + 1, store.hash_bytes(b"right")) is None


def test_member_names_are_unique_per_group_ignoring_case(db):
    group, _ = make_group(db, names=["Alice"])
    with pytest.raises(DuplicateMemberError):
        store.add_member(db, group.id, "alice")  # 콜레이션 ai_ci: 대소문자 무시
    other, _ = make_group(db, names=["Alice"], token=b"other")  # 다른 그룹은 같은 이름 가능
    assert [x.name for x in store.get_members(db, other.id)] == ["Alice"]


# ---------------------------------------------------------------- 기록과 잔액


def test_expense_is_stored_with_lines_and_balances(db):
    group, ids = make_group(db)
    split = EqualSplit([m(ids, "a"), m(ids, "b"), m(ids, "c")])
    result = post(db, group, expense_entry(m(ids, "a"), krw(10_000), split, "저녁"))

    entry = result.entry
    assert not result.replayed
    assert entry.memo == "저녁"
    # 10,000 / 3 = 3,334 + 3,333 + 3,333 (나머지 1원은 최대 나머지 방식)
    debits = sorted(str(ln.amount.amount) for ln in entry.lines if ln.side.value == "DEBIT")
    assert debits == ["3333", "3333", "3334"]
    balances = store.balances(db, group)
    assert sum(v.minor_units for v in balances.values()) == 0
    assert balances[ids["a"]] == krw(10_000 - 3_334)


def test_member_without_entries_has_zero_balance(db):
    group, ids = make_group(db)
    assert all(v.is_zero() for v in store.balances(db, group).values())
    assert set(store.balances(db, group)) == set(ids.values())


def test_reversal_cancels_and_only_once(db):
    group, ids = make_group(db)
    posted = post(db, group, transfer_entry(m(ids, "a"), m(ids, "b"), krw(5_000))).entry
    rev = reverse(db, group, posted.id).entry

    assert rev.reverses_entry_id == posted.id
    assert all(v.is_zero() for v in store.balances(db, group).values())
    assert store.list_entries(db, group.id, 10)[1].reversed_by_entry_id == rev.id

    with pytest.raises(AlreadyReversedError):
        reverse(db, group, posted.id)  # 두 번 취소
    with pytest.raises(AlreadyReversedError):
        reverse(db, group, rev.id)  # 취소의 취소
    with pytest.raises(EntryNotFoundError):
        reverse(db, group, 10**15)  # 정리 후에도 id는 계속 늘어나므로 큰 값
    assert count(db, journal_entries) == 2


def test_cannot_touch_another_groups_entries_or_members(db):
    group, ids = make_group(db)
    other, other_ids = make_group(db, token=b"other")
    posted = post(db, group, transfer_entry(m(ids, "a"), m(ids, "b"), krw(1_000))).entry

    with pytest.raises(EntryNotFoundError):
        reverse(db, other, posted.id)
    with pytest.raises(UnknownMemberError):
        # other 그룹에 group의 멤버로 기록
        post(db, other, transfer_entry(m(other_ids, "a"), m(ids, "b"), krw(1_000)))
    assert count(db, journal_entries) == 1  # 실패한 쓰기는 아무것도 남기지 않는다


def test_failed_check_writes_nothing(db):
    group, ids = make_group(db)
    with pytest.raises(InvalidSplitError):
        split = FixedSplit({m(ids, "a"): krw(1), m(ids, "b"): krw(1)})
        post(db, group, expense_entry(m(ids, "a"), krw(3), split))
    assert count(db, journal_entries) == count(db, entry_lines) == count(db, idempotency_keys) == 0


def test_concurrent_reversal_is_stopped_by_unique_constraint(db, monkeypatch):
    """검사(아직 취소 안 됨)와 INSERT 사이에 다른 요청이 먼저 취소한 경우를 흉내 낸다.

    _is_reversed가 항상 False를 돌려주게 해 코어 검사를 통과시키면, 두 번째 역분개는
    uq_journal_entries_reverses_entry_id에 막혀야 한다.
    """
    group, ids = make_group(db)
    posted = post(db, group, transfer_entry(m(ids, "a"), m(ids, "b"), krw(1_000))).entry
    reverse(db, group, posted.id)
    monkeypatch.setattr(store, "_is_reversed", lambda _conn, _id: False)
    with pytest.raises(AlreadyReversedError):
        reverse(db, group, posted.id)
    assert count(db, journal_entries) == 2


# ---------------------------------------------------------------- DB 제약 (코어를 거치지 않은 쓰기)


_raw_seq = iter(range(1, 1_000_000))


def _raw_entry(db: Engine, group_id: int, **values) -> int:
    values.setdefault("seq", next(_raw_seq))
    with db.begin() as conn:
        result = conn.execute(insert(journal_entries).values(group_id=group_id, **values))
        return int(result.inserted_primary_key[0])


def _error_code(exc: DBAPIError) -> int:
    return exc.orig.args[0]


@pytest.mark.parametrize(
    ("side", "amount"),
    [("DEBIT", 0), ("DEBIT", -100), ("LEFT", 100)],
)
def test_db_rejects_invalid_lines(db, side, amount):
    group, ids = make_group(db)
    entry_id = _raw_entry(db, group.id, kind="EXPENSE")
    with pytest.raises(DBAPIError) as info, db.begin() as conn:
        conn.execute(
            insert(entry_lines).values(
                group_id=group.id,
                entry_id=entry_id,
                line_no=1,
                member_id=ids["a"],
                side=side,
                amount=amount,
            )
        )
    assert _error_code(info.value) == ER_CHECK_CONSTRAINT_VIOLATED


def test_db_rejects_line_for_member_of_another_group(db):
    group, _ = make_group(db)
    _, other_ids = make_group(db, token=b"other")
    entry_id = _raw_entry(db, group.id, kind="EXPENSE")
    with pytest.raises(DBAPIError) as info, db.begin() as conn:
        conn.execute(
            insert(entry_lines).values(
                group_id=group.id,
                entry_id=entry_id,
                line_no=1,
                member_id=other_ids["a"],
                side="DEBIT",
                amount=100,
            )
        )
    assert _error_code(info.value) == ER_NO_REFERENCED_ROW


def test_db_requires_reversal_target_in_same_group(db):
    group, _ = make_group(db)
    other, _ = make_group(db, token=b"other")
    target = _raw_entry(db, group.id, kind="EXPENSE")

    with pytest.raises(DBAPIError) as info:
        _raw_entry(db, group.id, kind="REVERSAL")  # 대상 없는 역분개
    assert _error_code(info.value) == ER_CHECK_CONSTRAINT_VIOLATED
    with pytest.raises(DBAPIError) as info:
        _raw_entry(db, group.id, kind="EXPENSE", reverses_entry_id=target)  # 역분개가 아닌데 대상
    assert _error_code(info.value) == ER_CHECK_CONSTRAINT_VIOLATED
    with pytest.raises(DBAPIError) as info:
        _raw_entry(db, other.id, kind="REVERSAL", reverses_entry_id=target)  # 다른 그룹
    assert _error_code(info.value) == ER_NO_REFERENCED_ROW


def test_db_member_name_cannot_be_empty(db):
    group, _ = make_group(db, names=[])
    with pytest.raises(DBAPIError) as info, db.begin() as conn:
        conn.execute(insert(members).values(group_id=group.id, name=""))
    assert _error_code(info.value) == ER_CHECK_CONSTRAINT_VIOLATED


# ---------------------------------------------------------------- SQL 잔액 = 메모리 원장 잔액


@pytest.mark.parametrize("seed", range(3))
def test_sql_balances_match_in_memory_ledger(db, seed):
    """같은 무작위 거래(지출 3종, 송금, 취소)를 DB와 메모리 원장에 넣고 매번 잔액을 비교한다."""
    rng = random.Random(seed)
    group, ids = make_group(db, names=[f"p{i}" for i in range(6)], token=f"s{seed}".encode())
    keys = [member_key(i) for i in ids.values()]
    ledger = Ledger(KRW, keys)
    db_ids: dict[int, int] = {}  # 메모리 원장 번호 -> DB 분개 id

    for _ in range(40):
        op = rng.random()
        if op < 0.2 and ledger.entries:
            target = rng.choice(ledger.entries)
            if target.entry.kind.value == "REVERSAL" or ledger.is_reversed(target.entry_id):
                continue
            mem = ledger.reverse(target.entry_id)
            db_ids[mem.entry_id] = reverse(db, group, db_ids[target.entry_id]).entry.id
            continue
        payer, *others = rng.sample(keys, rng.randint(2, 6))
        amount = krw(rng.randint(1, 200) * 100 + rng.randint(0, 99))
        if op < 0.4:
            entry = transfer_entry(payer, others[0], amount)
        else:
            people = [payer, *others]
            kind = rng.choice(["equal", "ratio", "fixed"])
            if kind == "equal":
                split = EqualSplit(people)
            elif kind == "ratio":
                split = RatioSplit({p: rng.randint(0, 5) for p in people[:-1]} | {people[-1]: 1})
            else:
                cut = rng.randint(0, amount.minor_units)
                split = FixedSplit({people[0]: krw(cut), people[1]: krw(amount.minor_units - cut)})
            entry = expense_entry(payer, amount, split)
        mem = ledger.post(entry)
        db_ids[mem.entry_id] = post(db, group, entry).entry.id

        sql = {member_key(k): v for k, v in store.balances(db, group).items()}
        assert sql == ledger.balances()
    assert total_balance(ledger.balances(), KRW).is_zero()


# ---------------------------------------------------------------- 멱등성


def test_same_key_returns_first_entry_without_writing(db):
    group, ids = make_group(db)
    entry = transfer_entry(m(ids, "a"), m(ids, "b"), krw(1_000))
    first = post(db, group, entry, key="same")
    again = post(db, group, entry, key="same")
    assert again.replayed and again.entry == first.entry
    assert count(db, journal_entries) == 1


def test_same_key_with_different_request_is_rejected(db):
    group, ids = make_group(db)
    entry = transfer_entry(m(ids, "a"), m(ids, "b"), krw(1_000))
    store.post_entry(db, group, lambda _c, _s: entry, "k", store.hash_bytes(b"request-1"))
    with pytest.raises(IdempotencyKeyReusedError):
        store.post_entry(db, group, lambda _c, _s: entry, "k", store.hash_bytes(b"request-2"))


def test_keys_are_scoped_per_group_and_case_sensitive(db):
    group, ids = make_group(db)
    other, other_ids = make_group(db, token=b"other")
    post(db, group, transfer_entry(m(ids, "a"), m(ids, "b"), krw(1)), key="abc")
    post(db, group, transfer_entry(m(ids, "a"), m(ids, "b"), krw(1)), key="ABC")
    post(db, other, transfer_entry(m(other_ids, "a"), m(other_ids, "b"), krw(1)), key="abc")
    assert count(db, journal_entries) == 3


def test_concurrent_requests_with_same_key_create_one_entry(db):
    """같은 키의 요청 여러 개가 동시에 들어와도 분개는 하나. 늦은 요청은 첫 결과를 받는다.

    모든 스레드가 함께 출발해 "키 없음"(잠금 없는 빠른 확인)을 통과하게 하고, 그룹 잠금을 먼저 잡은
    요청이 분개를 만드는 동안 잠시 머물게 한다. 나머지는 잠금에서 기다렸다가, 잠금을 얻은 뒤의
    재확인에서 먼저 커밋된 키를 보고 처음 결과를 돌려받아야 한다.
    """
    group, ids = make_group(db)
    entry = transfer_entry(m(ids, "a"), m(ids, "b"), krw(1_000))
    n = 4
    start = threading.Barrier(n)
    results, errors = [], []
    built = []

    def build(_conn, _seq):
        built.append(1)
        time.sleep(0.3)  # 다른 요청들이 빠른 확인을 지나 잠금에서 기다리게 한다
        return entry

    def worker():
        try:
            start.wait(timeout=10)
            results.append(store.post_entry(db, group, build, "dup", store.hash_bytes(b"same")))
        except Exception as exc:  # pragma: no cover - 실패 시 원인 표시용
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert len({r.entry.id for r in results}) == 1
    assert sum(not r.replayed for r in results) == 1
    assert count(db, journal_entries) == 1
    assert count(db, entry_lines) == 2
    assert len(built) == 1  # 분개를 만든 것은 처음 요청 하나뿐


# ---------------------------------------------------------------- 다중 통화 (5단계)


@pytest.mark.parametrize(
    "constraint", ["ck_settlement_groups_currency", "ck_journal_entries_original_currency"]
)
def test_db_currency_checks_match_app_currencies(db, constraint):
    """alembic check는 CHECK 내용을 비교하지 않는다.
    통화를 늘리고 마이그레이션을 빠뜨리면 여기서 잡는다."""
    with db.connect() as conn:
        clause = conn.execute(
            text(
                "SELECT CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS"
                " WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_NAME = :name"
            ),
            {"name": constraint},
        ).scalar_one()
    # MySQL이 저장한 식은 소문자로 바뀐다: (`currency` in (_utf8mb4'CNY',...)).
    # 그래서 대문자 세 글자는 통화 코드뿐이다.
    assert set(re.findall(r"[A-Z]{3}", clause)) == set(CURRENCIES)


@pytest.mark.parametrize(
    "values",
    [
        {"original_currency": "USD"},  # 셋 중 하나만
        {"original_currency": "USD", "original_amount": 10, "fx_rate": 0},  # 환율 0
        {"original_currency": "XXX", "original_amount": 10, "fx_rate": 1},  # 모르는 통화
        {"kind": "TRANSFER", "original_currency": "USD", "original_amount": 10, "fx_rate": 1},
    ],
)
def test_db_rejects_inconsistent_fx_columns(db, values):
    group, _ = make_group(db)
    values = {"kind": "EXPENSE", **values}
    with pytest.raises(DBAPIError) as info:
        _raw_entry(db, group.id, **values)
    assert _error_code(info.value) == ER_CHECK_CONSTRAINT_VIOLATED
