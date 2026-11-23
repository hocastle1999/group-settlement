"""예외 -> HTTP 상태 코드.

코어는 HTTP를 모른다. 코어 예외(`LedgerError`)와 저장 계층 예외(`StoreError`)를
여기서 한 번에 바꾼다.
핸들러마다 try/except를 쓰지 않으므로 새 엔드포인트도 같은 규칙을 따른다.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.db.store import (
    DuplicateMemberError,
    IdempotencyKeyReusedError,
    SettlementConflictError,
    SettlementNotFoundError,
    StaleVersionError,
    StoreError,
    TransferAlreadyCompletedError,
)
from app.ledger import (
    AlreadyReversedError,
    CurrencyMismatchError,
    EntryNotFoundError,
    InvalidAmountError,
    InvalidExchangeRateError,
    InvalidSplitError,
    LedgerError,
    TooManyMembersError,
    UnbalancedBalancesError,
    UnbalancedEntryError,
    UnknownMemberError,
)

logger = logging.getLogger(__name__)

# 위에서부터 isinstance로 찾는다. 하위 클래스를 상위 클래스보다 먼저 둔다.
STATUS_BY_ERROR: list[tuple[type[Exception], int]] = [
    # 요청 내용이 원장 규칙에 어긋난다 (형식은 맞지만 처리할 수 없는 내용)
    (InvalidAmountError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (InvalidExchangeRateError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (InvalidSplitError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (UnbalancedEntryError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (CurrencyMismatchError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (UnknownMemberError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (TooManyMembersError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (EntryNotFoundError, status.HTTP_404_NOT_FOUND),
    (SettlementNotFoundError, status.HTTP_404_NOT_FOUND),
    # 지금 상태와 충돌한다: 이미 취소됨, 이름 중복
    (AlreadyReversedError, status.HTTP_409_CONFLICT),
    (DuplicateMemberError, status.HTTP_409_CONFLICT),
    # 확정하려던 버전 뒤에 분개가 더 생김 / 같은 버전이 다른 방법으로 이미 확정됨
    (StaleVersionError, status.HTTP_409_CONFLICT),
    (SettlementConflictError, status.HTTP_409_CONFLICT),
    (TransferAlreadyCompletedError, status.HTTP_409_CONFLICT),
    # 같은 키를 다른 요청에 재사용 (IETF Idempotency-Key 초안의 권고)
    (IdempotencyKeyReusedError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    # 원장이 깨졌다: 클라이언트 잘못이 아니라 서버 버그
    (UnbalancedBalancesError, status.HTTP_500_INTERNAL_SERVER_ERROR),
]


def status_for(exc: Exception) -> int:
    for error_type, code in STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return code
    return status.HTTP_500_INTERNAL_SERVER_ERROR


async def _handle(request: Request, exc: Exception) -> JSONResponse:
    code = status_for(exc)
    if code >= 500:
        # 내부 사정(잔액 합 등)은 로그에만 남기고 응답에는 넣지 않는다.
        logger.error("원장 오류 %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse({"error": "InternalError", "detail": "서버 오류"}, status_code=code)
    body = {"error": type(exc).__name__, "detail": str(exc)}
    # 클라이언트가 다음 행동을 정하는 데 필요한 값(예: 현재 원장 버전)은 별도 필드로 준다.
    body.update(getattr(exc, "extra", {}))
    return JSONResponse(body, status_code=code)


def install(app: FastAPI) -> None:
    app.add_exception_handler(LedgerError, _handle)
    app.add_exception_handler(StoreError, _handle)
