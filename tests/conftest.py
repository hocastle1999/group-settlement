import os
from collections.abc import Iterator
from pathlib import Path

# 앱 모듈을 import하기 "전에" 테스트 DB를 정한다 (app.main은 import할 때 앱을 만든다).
# 환경 변수는 .env보다 우선한다. 이름이 _test로 끝나는 DB만 쓴다 (아래 db_settings).
os.environ["DB_NAME"] = os.environ.get("TEST_DB_NAME", "group_settlement_test")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from hypothesis import settings  # noqa: E402
from sqlalchemy import Engine, text  # noqa: E402

from app.db.config import DatabaseSettings, create_db_engine  # noqa: E402
from app.db.tables import metadata  # noqa: E402
from app.main import create_app  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# CI에서는 무작위 시드를 고정해(derandomize) 같은 커밋이면 항상 같은 입력으로 돌게 한다.
# 로컬에서는 매번 새 입력을 시도해 반례를 더 넓게 찾는다.
settings.register_profile("ci", derandomize=True, max_examples=300)
settings.register_profile("dev", max_examples=200)
settings.load_profile("ci" if os.environ.get("CI") else "dev")


@pytest.fixture(scope="session")
def db_settings() -> DatabaseSettings:
    db = DatabaseSettings()
    # 안전장치: 실수로 개발용 DB를 지우지 않도록 이름이 _test로 끝나는 DB만 허용한다.
    assert db.db_name.endswith("_test"), f"테스트는 *_test DB에서만 (현재: {db.db_name})"
    return db


@pytest.fixture(scope="session")
def db_engine(db_settings: DatabaseSettings) -> Iterator[Engine]:
    """테스트 세션에서 한 번: 스키마를 마이그레이션으로 새로 만든다.

    base까지 내렸다가 head로 올리므로 마이그레이션의 왕복도 함께 검증된다.
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_db_engine(db_settings)
    yield engine
    engine.dispose()


def _clear(engine: Engine) -> None:
    # 원장 테이블은 서로를(역분개는 자기 테이블을) 참조하므로 외래 키 검사를 잠시 끄고 비운다.
    # TRUNCATE는 쓰지 않는다: InnoDB는 테이블 파일을 지우고 다시 만들어 5개에 약 1초가 걸렸다.
    # 몇 줄뿐인 테스트 데이터는 DELETE가 2ms (docs/troubleshooting.md).
    # 테스트 정리용일 뿐, 앱 코드는 원장에 DELETE를 하지 않는다.
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in reversed(metadata.sorted_tables):
            conn.execute(text(f"DELETE FROM {table.name}"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Engine]:
    """DB 테스트 하나마다 빈 테이블에서 시작한다.

    저장 코드가 스스로 트랜잭션을 열고 커밋하므로(engine.begin), 롤백 대신 비워서 정리한다.
    """
    _clear(db_engine)
    yield db_engine


@pytest.fixture
def client(db: Engine, db_settings: DatabaseSettings) -> Iterator[TestClient]:
    app = create_app(db_settings)
    app.state.engine = db  # 테스트와 같은 엔진을 써서 SQL 감시(이벤트)도 같은 곳에 건다
    with TestClient(app) as c:
        yield c
