"""원장 코어의 예외. API 단계(3단계)에서 HTTP 상태 코드로 바꾼다."""


class LedgerError(Exception):
    """원장 코어 예외의 공통 부모."""


class InvalidAmountError(LedgerError):
    """금액 형식이나 정밀도가 잘못됐다 (float, 1.5원 등)."""


class CurrencyMismatchError(LedgerError):
    """서로 다른 통화를 섞어 계산하려 했다."""


class InvalidSplitError(LedgerError):
    """분할 규칙이 잘못됐다 (참여자 없음, 정액 합계 불일치 등)."""


class UnbalancedEntryError(LedgerError):
    """분개의 차변 합계와 대변 합계가 다르다."""


class UnknownMemberError(LedgerError):
    """원장에 없는 멤버에게 기록하려 했다."""


class EntryNotFoundError(LedgerError):
    """없는 분개 번호."""


class AlreadyReversedError(LedgerError):
    """이미 취소(역분개)된 분개를 다시 취소하려 했다."""


class UnbalancedBalancesError(LedgerError):
    """정산에 넘긴 잔액의 합이 0이 아니다. 원장이 올바르면 일어나지 않는다."""


class TooManyMembersError(LedgerError):
    """최적 정산을 계산하기에는 잔액이 남은 사람이 너무 많다."""
