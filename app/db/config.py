"""DB 접속 설정. .env(DB_*)에서 읽는다. 비밀번호는 .env에만 둔다."""

import ssl

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, Engine, create_engine


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "group_settlement"
    db_user: str = "group_settlement_app"
    # SecretStr: 로그나 오류 메시지에 설정 객체가 찍혀도 '**********'로 가려진다.
    db_password: SecretStr = SecretStr("")
    # 외부(클라우드) MySQL은 대개 TLS 연결만 받는다. 켜면 서버 인증서와 호스트 이름을 검증한다.
    db_ssl: bool = False
    # 연결 풀. 무료 MySQL은 동시 연결 수가 작다(수십 개 이하).
    # 서버 하나 기준 최대 연결 = size + overflow.
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)

    @property
    def database_url(self) -> URL:
        # 문자열을 이어 붙이지 않고 URL.create를 쓴다: 비밀번호의 @, :, / 등을 알아서 처리한다.
        return URL.create(
            drivername="mysql+pymysql",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": "utf8mb4"},
        )

    @property
    def connect_args(self) -> dict[str, object]:
        """연결마다 세션 시간대를 UTC로 고정한다 (로컬 MySQL은 PC 시간대를 따르기 때문).

        앱 엔진과 Alembic(migrations/env.py)이 같은 값을 쓴다.
        """
        args: dict[str, object] = {"init_command": "SET time_zone = '+00:00'"}
        if self.db_ssl:
            # 시스템의 신뢰 인증서로 서버 인증서를 검증하고 호스트 이름도 확인한다(기본 컨텍스트).
            # 검증을 끄면(CERT_NONE) 암호화는 되지만 중간자가 가짜 서버로 끼어들 수 있다.
            args["ssl"] = ssl.create_default_context()
        return args


def create_db_engine(settings: DatabaseSettings) -> Engine:
    """create_engine은 이 시점에 접속하지 않는다. 첫 쿼리 때 연결 풀에서 연결을 만든다.

    pool_pre_ping: MySQL이 오래 쉬던 연결을 끊은 경우(wait_timeout)에 대비해, 꺼내기 전에
    살아 있는지 확인한다.
    """
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # 클라우드 DB나 중간 장비가 오래 쉬는 연결을 먼저 끊을 수 있어 5분 넘은 연결은 새로 맺는다.
        pool_recycle=300,
        connect_args=settings.connect_args,
    )
