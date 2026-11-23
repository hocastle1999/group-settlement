# group-settlement

모임 경비 정산 서비스. 여행이나 회식에서 누가 얼마를 냈는지 기록하면,
누가 누구에게 얼마를 보내면 끝나는지 계산한다. 백엔드 포트폴리오 3번째 프로젝트.

- 1번 [reservation-api](https://github.com/hocastle1999/reservation-api): 트랜잭션과 락
- 2번 [smart-elevator](https://github.com/hocastle1999/smart-elevator): 실시간 처리, 상태 머신, 알고리즘
- 3번 (이 프로젝트): **돈을 정확하게 다루는 설계**. 이중기입 원장, 정밀한 금액 계산, 최소 송금 알고리즘

## 핵심 설계

| 문제 | 선택 | 이유 |
| --- | --- | --- |
| 금액 표현 | `Decimal` + 통화별 소수 자릿수 | float는 `0.1 + 0.2 != 0.3`. 1.5원 같은 값은 만들 수 없게 막는다 |
| 나눠떨어지지 않는 금액 | 최소 단위 정수로 나누고 남는 단위를 최대 나머지 방식으로 배분 | 10,000원 / 3 = 3,334 + 3,333 + 3,333. 합이 항상 원래 금액과 같다 |
| 거래 기록 | 이중기입 원장 (차변 합 = 대변 합) | 모든 멤버 잔액의 합이 언제나 0. 계산 실수가 구조적으로 드러난다 |
| 잘못 기록한 거래 | 수정·삭제 없이 역분개 추가 (append-only) | 무엇이 언제 바뀌었는지 기록 자체에 남는다 |
| 정산 송금 | 16명 이하(`SETTLE_OPTIMAL_LIMIT`)는 비트마스크 DP 최적해, 넘으면 그리디 | 1/N처럼 둥근 금액이면 그리디가 1 ~ 2번 더 보낸다. DP는 16명 37ms ([실험](docs/experiments.md)) |
| 인증 | 회원가입 없이 그룹별 초대 링크 토큰, DB에는 SHA-256 해시만 | 실제 정산 앱의 사용 방식. 틀린 토큰도 없는 그룹도 같은 404 (그룹 존재 여부를 숨김) |
| 재시도 | 분개 쓰기에 `Idempotency-Key` 필수, 키와 분개를 한 트랜잭션에 | 응답을 못 받고 다시 보내도 지출이 두 번 기록되지 않는다. 동시 재시도도 유니크 잠금으로 하나만 |
| DB 정합성 | `CHECK (amount > 0)`, 역분개 대상 유니크, 복합 외래 키 | 코어를 거치지 않은 쓰기나 동시 요청도 규칙을 못 깬다 |
| 동시 쓰기 | 그룹 행 `FOR UPDATE` + 그룹별 순번, 정산 확정은 원장 버전 비교 | id 순서는 커밋 순서가 아니다. "확정한 계획 = 그 버전의 원장"이 동시 지출에도 지켜진다 |
| 외화 | 지출 시점 환율로 총액을 한 번 환산, 원래 금액·환율은 증빙 | 원장은 통화 하나라 기존 규칙이 그대로. 사람마다 환산하면 반올림이 쌓여 합이 어긋난다 |
| 잔액 | 매번 SUM + 커버링 인덱스 (잔액 테이블 없음) | 실험: 잔액 테이블은 빠르지만 갱신 손실, 데드락, 두 원천의 어긋남을 관리해야 한다 |

자세한 내용은 [docs/design.md](docs/design.md).

## 로드맵

1. [x] **금액·원장 코어** (순수 파이썬): `Money`, 분할 규칙(균등·비율·정액), 이중기입 분개, 역분개, property-based 테스트
2. [x] **최소 송금 알고리즘**: 그리디 vs 비트마스크 DP 최적해. 둥근 금액 12명 모임에서 그리디가 64% 더 많이 보냄 ([실험](docs/experiments.md))
3. [x] **DB 스키마 + REST API**: MySQL, 추가만 하는 원장 테이블, CHECK 제약, 멱등성 키, 초대 링크 토큰. 규칙은 코어를 재사용하고 SQL 잔액을 메모리 원장과 비교 테스트
4. [x] **동시성·정합성**: 그룹별 순번과 행 잠금(AUTO_INCREMENT ≠ 커밋 순서), 버전을 확인하는 정산 확정(409), 매번 합산 vs 잔액 테이블 실험 -> 커버링 인덱스로 1만 줄 25ms -> 7ms ([실험](docs/experiments.md#4단계-잔액-계산-방식과-동시성))
5. [x] **다중 통화**: 지출 시점 환율로 기준 통화에 한 번 환산(ROUND_HALF_EVEN), 원래 금액과 환율은 증빙으로 저장, 외화 정액 분할도 합이 정확. 남는 1원을 분개 순번만큼 돌려 배분
6. [ ] **정산 확정·송금 완료**: 감사 로그, 리포트 SQL, 데모 페이지
7. [ ] **배포**

## 실행 (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# DB 준비 (최초 1회)
# 1) MySQL 8.0에서 root로 scripts/init_db.sql 실행 ('여기에_비밀번호'를 바꿔서)
# 2) .env.example을 .env로 복사하고 DB_PASSWORD에 같은 비밀번호
Copy-Item .env.example .env
.\.venv\Scripts\alembic.exe upgrade head

.\.venv\Scripts\python.exe -m pytest -q                    # 테스트 (group_settlement_test DB 사용)
.\.venv\Scripts\python.exe -m uvicorn app.main:app         # API 서버, 문서: http://localhost:8000/docs
.\.venv\Scripts\python.exe -m scripts.compare_settlement   # 그리디 vs 최적 실험
```

| 환경 변수 (`.env`) | 기본값 | 설명 |
| --- | --- | --- |
| `DB_HOST` | `127.0.0.1` | |
| `DB_PORT` | `3306` | |
| `DB_NAME` | `group_settlement` | 테스트는 `TEST_DB_NAME`(기본 `group_settlement_test`), 이름이 `_test`로 끝나야 실행 |
| `DB_USER` | `group_settlement_app` | |
| `DB_PASSWORD` | (없음) | `scripts/init_db.sql`에 넣은 값 |
| `SETTLE_OPTIMAL_LIMIT` | `16` | 정산 AUTO가 최적(비트마스크 DP)을 쓰는 최대 인원 (1 ~ 20) |

## 사용 예 (REST API)

Windows PowerShell 5.1에서는 `-ContentType`에 `charset=utf-8`을 꼭 붙인다. 없으면 한글 본문이 `?`로 바뀌어 저장된다 ([문제 해결](docs/troubleshooting.md)).

```powershell
$json = "application/json; charset=utf-8"
# 그룹 생성: invite_token은 이 응답에서만 볼 수 있다
$g = Invoke-RestMethod -Method Post http://localhost:8000/api/groups -ContentType $json `
  -Body '{"name": "제주 여행", "currency": "KRW"}'
$h = @{ Authorization = "Bearer $($g.invite_token)" }
$base = "http://localhost:8000/api/groups/$($g.id)"

$a = Invoke-RestMethod -Method Post "$base/members" -Headers $h -ContentType $json -Body '{"name": "민수"}'
$b = Invoke-RestMethod -Method Post "$base/members" -Headers $h -ContentType $json -Body '{"name": "지영"}'

# 지출: 금액은 문자열, 재시도에 대비해 Idempotency-Key 필수
$body = @{ payer_id = $a.id; amount = "30000"; memo = "저녁";
           split = @{ type = "equal"; member_ids = @($a.id, $b.id) } } | ConvertTo-Json
Invoke-RestMethod -Method Post "$base/expenses" -ContentType $json -Body $body `
  -Headers ($h + @{ "Idempotency-Key" = [guid]::NewGuid().ToString() })

# 외화 지출: 엔화 3,000엔을 1엔 = 9.1234원으로. 원장에는 27,370원으로 기록된다
$fx = @{ payer_id = $b.id; amount = "3000"; currency = "JPY"; fx_rate = "9.1234";
         split = @{ type = "equal"; member_ids = @($a.id, $b.id) } } | ConvertTo-Json
Invoke-RestMethod -Method Post "$base/expenses" -ContentType $json -Body $fx `
  -Headers ($h + @{ "Idempotency-Key" = [guid]::NewGuid().ToString() })

(Invoke-RestMethod "$base/balances" -Headers $h).balances       # 민수 1315, 지영 -1315
(Invoke-RestMethod "$base/settlement" -Headers $h).transfers    # 지영 -> 민수 1315
```

## 사용 예 (1단계 코어)

```python
from app.ledger import (
    EqualSplit,
    Ledger,
    Money,
    expense_entry,
    get_currency,
    settle,
    transfer_entry,
)

ledger = Ledger(get_currency("KRW"), ["민수", "지영", "현우"])
ledger.post(
    expense_entry("민수", Money.of(30_000, "KRW"), EqualSplit(["민수", "지영", "현우"]), "저녁")
)
{m: str(v) for m, v in ledger.balances().items()}
# {'민수': '20000 KRW', '지영': '-10000 KRW', '현우': '-10000 KRW'}  (합은 항상 0)

ledger.post(transfer_entry("지영", "민수", Money.of(10_000, "KRW")))
# 잔액: 민수 10000, 지영 0, 현우 -10000

settle(ledger.balances())  # 남은 정산: 현우 -> 민수 10000
```
