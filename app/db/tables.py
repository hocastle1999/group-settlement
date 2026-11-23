"""테이블 정의 (SQLAlchemy Core).

ORM 대신 Core를 쓰는 이유: 원장은 한 번 쓰고 고치지 않는 기록이다(append-only).
객체 상태를 추적해 UPDATE를 만들어 주는 ORM(Unit of Work)의 장점이 필요 없고,
잔액 SUM 같은 집계 SQL을 직접 쓰는 편이 읽기 쉽다.

"같은 그룹 안에서만"이라는 규칙은 복합 외래 키로 DB가 보장한다.
줄(entry_lines)과 멱등성 키에도 group_id를 두고 (group_id, 분개 id), (group_id, 멤버 id)를
가리키게 하면, 다른 그룹의 멤버나 분개를 가리키는 행은 INSERT 자체가 실패한다.
"""

from sqlalchemy import (
    BINARY,
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME

from app.ledger import CURRENCIES, EntryKind, Side, Strategy

# 제약과 인덱스 이름 규칙. 이름이 정해져 있어야 마이그레이션에서 지우거나 바꿀 수 있다.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _in(column: str, values: list[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


# 금액 열. DECIMAL(19, 4): 정수부 15자리, 소수 4자리. 지금 통화는 소수 2자리까지지만
# 다중 통화(5단계)에서 소수 3자리 통화(BHD 등)와 환산 금액을 담을 여유를 둔다.
# float/double은 쓰지 않는다 (0.1을 정확히 저장할 수 없다).
AMOUNT = Numeric(19, 4)
# 환율 열. 정수 10자리 + 소수 10자리 (app/ledger/fx.py의 RATE_MAX_* 와 같다).
FX_RATE = Numeric(20, 10)

# ENUM 대신 VARCHAR + CHECK: MySQL ENUM은 값을 추가할 때 ALTER TABLE이 필요하고 내부적으로
# 순번으로 정렬되는 등 헷갈린다. CHECK는 MySQL 8.0.16부터 실제로 검사한다.
CURRENCY_CODES = sorted(CURRENCIES)
ENTRY_KINDS = [k.value for k in EntryKind]
SIDES = [s.value for s in Side]

# GROUPS는 MySQL 8.0의 예약어(윈도 함수 프레임)라 테이블 이름으로 쓰지 않는다.
settlement_groups = Table(
    "settlement_groups",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("name", String(100), nullable=False),
    Column("currency", String(3), nullable=False),
    # 초대 토큰의 SHA-256 해시(32바이트). 토큰 원문은 저장하지 않는다 (app/api/auth.py).
    Column("invite_token_hash", BINARY(32), nullable=False),
    # 이 그룹의 마지막 분개 순번 = 원장 버전. 분개를 쓰는 트랜잭션은 이 행을 FOR UPDATE로 잠그고
    # 1 늘린다. 그래서 같은 그룹의 쓰기는 한 줄로 서고, 순번 순서가 곧 커밋 순서가 된다 (store.py).
    Column("last_seq", BigInteger, nullable=False, server_default="0"),
    # 감사 로그(6단계)의 마지막 순번과 마지막 행의 해시. 끝부분이 잘려 나가도 드러나게 하는 기준점.
    Column("audit_seq", BigInteger, nullable=False, server_default="0"),
    Column("audit_head", BINARY(32), nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("invite_token_hash"),
    CheckConstraint(_in("currency", CURRENCY_CODES), name="currency"),
    CheckConstraint("name <> ''", name="name_not_empty"),
    CheckConstraint("last_seq >= 0", name="last_seq_not_negative"),
)

members = Table(
    "members",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("group_id", BigInteger, nullable=False),
    # 콜레이션 utf8mb4_0900_ai_ci: 대소문자를 구분하지 않으므로 "Alice"와 "alice"는 같은 이름이다.
    Column("name", String(50), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    ForeignKeyConstraint(["group_id"], ["settlement_groups.id"]),
    UniqueConstraint("group_id", "name"),
    # 복합 외래 키의 대상. id만으로도 유일하지만 (group_id, id) 인덱스가 있어야 참조할 수 있다.
    UniqueConstraint("group_id", "id"),
    CheckConstraint("name <> ''", name="name_not_empty"),
)

journal_entries = Table(
    "journal_entries",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("group_id", BigInteger, nullable=False),
    # 그룹 안 순번 (1, 2, 3, ... 빈 번호 없음). id(AUTO_INCREMENT)는 "번호를 받은 순서"일 뿐
    # 커밋 순서가 아니어서 원장 버전으로 쓸 수 없다 (docs/design.md 4단계).
    Column("seq", BigInteger, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("memo", String(200), nullable=False, server_default=""),
    # REVERSAL이면 취소하는 분개 id. 유니크라서 한 분개는 한 번만 취소된다.
    # (MySQL 유니크 인덱스는 NULL을 여러 개 허용한다 -> 일반 분개는 모두 NULL이어도 된다)
    Column("reverses_entry_id", BigInteger, nullable=True),
    # 외화 지출의 증빙 (5단계). 줄(entry_lines)은 모두 그룹 기준 통화로 환산된 금액이다.
    # 환율 = 원래 통화 1단위당 기준 통화 단위. 셋 다 있거나 셋 다 없다.
    Column("original_currency", String(3), nullable=True),
    Column("original_amount", AMOUNT, nullable=True),
    Column("fx_rate", FX_RATE, nullable=True),
    # 확정 정산의 송금 한 줄을 "보냈다"로 처리한 송금이면 그 줄 (6단계). 줄의 상태를 UPDATE하지 않고
    # 이 연결로 완료를 나타낸다. 완료 = 그 줄을 가리키는 송금이 있고 취소되지 않았다.
    Column("settlement_id", BigInteger, nullable=True),
    Column("settlement_line_no", SmallInteger, nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    ForeignKeyConstraint(["group_id"], ["settlement_groups.id"]),
    UniqueConstraint("group_id", "id"),
    UniqueConstraint("group_id", "seq"),
    UniqueConstraint("reverses_entry_id"),
    CheckConstraint("seq > 0", name="seq_positive"),
    # 같은 그룹의 분개만 취소할 수 있다. reverses_entry_id가 NULL이면 검사하지 않는다(MATCH SIMPLE).
    ForeignKeyConstraint(
        ["group_id", "reverses_entry_id"], ["journal_entries.group_id", "journal_entries.id"]
    ),
    CheckConstraint(_in("kind", ENTRY_KINDS), name="kind"),
    CheckConstraint(
        "(kind = 'REVERSAL') = (reverses_entry_id IS NOT NULL)", name="reversal_has_target"
    ),
    # CHECK에서 NULL은 "알 수 없음"이라 통과한다. 그래서 값이 있을 때의 규칙만 쓰면 된다.
    CheckConstraint(_in("original_currency", CURRENCY_CODES), name="original_currency"),
    CheckConstraint("original_amount > 0", name="original_amount_positive"),
    CheckConstraint("fx_rate > 0", name="fx_rate_positive"),
    CheckConstraint(
        "(original_currency IS NULL) = (original_amount IS NULL)"
        " AND (original_amount IS NULL) = (fx_rate IS NULL)",
        name="fx_all_or_none",
    ),
    CheckConstraint("original_currency IS NULL OR kind = 'EXPENSE'", name="fx_only_expense"),
    ForeignKeyConstraint(
        ["group_id", "settlement_id", "settlement_line_no"],
        [
            "settlement_transfers.group_id",
            "settlement_transfers.settlement_id",
            "settlement_transfers.line_no",
        ],
    ),
    Index(None, "group_id", "settlement_id", "settlement_line_no"),
    CheckConstraint(
        "(settlement_id IS NULL) = (settlement_line_no IS NULL)", name="settlement_link_both"
    ),
    CheckConstraint("settlement_id IS NULL OR kind = 'TRANSFER'", name="settlement_link_transfer"),
)

entry_lines = Table(
    "entry_lines",
    metadata,
    # 기본키 (group_id, entry_id, line_no): InnoDB는 기본키 순서로 행을 저장한다(클러스터형 인덱스).
    # 한 그룹의 줄이 디스크에 모여 있어 잔액 SUM(WHERE group_id = ?)이 연속 구간만 읽는다.
    # 또 기본키 앞부분 (group_id, entry_id)가 분개 외래 키의 인덱스 역할을 한다.
    Column("group_id", BigInteger, primary_key=True, autoincrement=False),
    Column("entry_id", BigInteger, primary_key=True, autoincrement=False),
    Column("line_no", SmallInteger, primary_key=True, autoincrement=False),
    Column("member_id", BigInteger, nullable=False),
    Column("side", String(6), nullable=False),
    # 금액은 항상 양수, 방향은 side. 부호 있는 금액 하나보다 잘못된 값을 CHECK로 막기 쉽다.
    Column("amount", AMOUNT, nullable=False),
    ForeignKeyConstraint(
        ["group_id", "entry_id"], ["journal_entries.group_id", "journal_entries.id"]
    ),
    ForeignKeyConstraint(["group_id", "member_id"], ["members.group_id", "members.id"]),
    # 커버링 인덱스: 잔액 SUM에 필요한 열(side, amount)까지 담아 인덱스만 읽고 끝난다.
    # 줄 1만 개에서 25ms -> 7ms, 100만 개에서 5.5s -> 0.7s (docs/experiments.md 4단계).
    # 앞부분 (group_id, member_id)가 멤버 외래 키의 인덱스 역할도 한다.
    Index(None, "group_id", "member_id", "side", "amount"),
    CheckConstraint(_in("side", SIDES), name="side"),
    CheckConstraint("amount > 0", name="amount_positive"),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    # 같은 키로 다시 온 쓰기 요청은 새 분개를 만들지 않고 처음 분개를 돌려준다 (app/db/store.py).
    Column("group_id", BigInteger, primary_key=True, autoincrement=False),
    # ascii_bin: 대소문자를 구분한다. 기본 콜레이션(ai_ci)이면 'abc'와 'ABC'가 같은 키가 된다.
    Column("idem_key", String(64, collation="ascii_bin"), primary_key=True),
    # 요청(경로 + 본문)의 SHA-256. 같은 키에 다른 요청이 오면 거부한다.
    Column("request_hash", BINARY(32), nullable=False),
    Column("entry_id", BigInteger, nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    ForeignKeyConstraint(["group_id"], ["settlement_groups.id"]),
    ForeignKeyConstraint(
        ["group_id", "entry_id"], ["journal_entries.group_id", "journal_entries.id"]
    ),
    Index(None, "group_id", "entry_id"),
)

# 확정된 정산(4단계). "원장 버전 version까지의 잔액으로 계산한 송금 계획"을 그대로 남긴다.
# 확정 기록도 고치거나 지우지 않는다. 송금 완료 처리는 6단계.
SETTLEMENT_STRATEGIES = [s.value for s in Strategy]

settlements = Table(
    "settlements",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("group_id", BigInteger, nullable=False),
    # 확정 시점의 원장 버전(settlement_groups.last_seq). 이 순번까지의 분개로 계산했다.
    Column("version", BigInteger, nullable=False),
    Column("strategy", String(16), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    ForeignKeyConstraint(["group_id"], ["settlement_groups.id"]),
    # 한 버전은 한 번만 확정한다. 같은 버전으로 다시 확정하면 처음 기록을 돌려준다(자연 키 멱등성).
    UniqueConstraint("group_id", "version"),
    UniqueConstraint("group_id", "id"),
    CheckConstraint("version >= 0", name="version_not_negative"),
    CheckConstraint(_in("strategy", SETTLEMENT_STRATEGIES), name="strategy"),
)

settlement_transfers = Table(
    "settlement_transfers",
    metadata,
    Column("group_id", BigInteger, primary_key=True, autoincrement=False),
    Column("settlement_id", BigInteger, primary_key=True, autoincrement=False),
    Column("line_no", SmallInteger, primary_key=True, autoincrement=False),
    Column("sender_member_id", BigInteger, nullable=False),
    Column("receiver_member_id", BigInteger, nullable=False),
    Column("amount", AMOUNT, nullable=False),
    ForeignKeyConstraint(["group_id", "settlement_id"], ["settlements.group_id", "settlements.id"]),
    ForeignKeyConstraint(["group_id", "sender_member_id"], ["members.group_id", "members.id"]),
    ForeignKeyConstraint(["group_id", "receiver_member_id"], ["members.group_id", "members.id"]),
    # 외래 키마다 인덱스가 있어야 한다. 직접 만들지 않으면 MySQL이 이름을 지어 만들고,
    # 그러면 모델(tables.py)과 DB가 달라 alembic check가 차이를 보고한다.
    Index(None, "group_id", "sender_member_id"),
    Index(None, "group_id", "receiver_member_id"),
    CheckConstraint("amount > 0", name="amount_positive"),
    CheckConstraint("sender_member_id <> receiver_member_id", name="different_members"),
)

# 감사 로그 (6단계). 누가(스스로 밝힌 멤버), 언제, 무엇을 했는지 한 줄씩. 추가만 한다.
# 바뀌는 내용과 같은 트랜잭션에 기록하고, 행마다 직전 행 해시를 이어 붙인 해시를 남긴다.
AUDIT_ACTIONS = [
    "GROUP_CREATED",
    "MEMBER_ADDED",
    "EXPENSE_RECORDED",
    "TRANSFER_RECORDED",
    "ENTRY_REVERSED",
    "SETTLEMENT_CONFIRMED",
    "TRANSFER_COMPLETED",
]

audit_events = Table(
    "audit_events",
    metadata,
    Column("group_id", BigInteger, primary_key=True, autoincrement=False),
    Column("seq", BigInteger, primary_key=True, autoincrement=False),  # 그룹 안 순번
    Column("action", String(32), nullable=False),
    Column("target_id", BigInteger, nullable=True),  # 분개, 멤버, 정산 id
    # 요청 헤더 X-Actor-Member-Id로 "누가 했다고 밝혔는지". 인증이 아니다 (docs/design.md 6단계).
    Column("actor_member_id", BigInteger, nullable=True),
    Column("detail", JSON, nullable=False),
    # 해시에 들어가는 값이라 앱이 정해 넣는다 (마이크로초까지, UTC).
    Column("created_at", DATETIME(fsp=6), nullable=False),
    Column("prev_hash", BINARY(32), nullable=False),
    Column("hash", BINARY(32), nullable=False),
    ForeignKeyConstraint(["group_id"], ["settlement_groups.id"]),
    ForeignKeyConstraint(["group_id", "actor_member_id"], ["members.group_id", "members.id"]),
    Index(None, "group_id", "actor_member_id"),
    CheckConstraint(_in("action", AUDIT_ACTIONS), name="action"),
    CheckConstraint("seq > 0", name="seq_positive"),
)
