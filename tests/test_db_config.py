"""DB 접속 설정 테스트 (DB 없이)."""

import ssl

from app.db.config import DatabaseSettings, create_db_engine


def test_tls_is_off_by_default_and_verifies_when_on():
    assert "ssl" not in DatabaseSettings(db_ssl=False).connect_args
    context = DatabaseSettings(db_ssl=True).connect_args["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_pool_size_comes_from_settings():
    engine = create_db_engine(DatabaseSettings(db_pool_size=2, db_max_overflow=1))
    try:
        assert engine.pool.size() == 2
        assert engine.pool._max_overflow == 1
    finally:
        engine.dispose()


def test_password_is_hidden_in_repr():
    settings = DatabaseSettings(db_password="secret-value")
    assert "secret-value" not in repr(settings)
    assert "secret-value" not in str(settings.database_url)  # URL 문자열에서도 가려진다
