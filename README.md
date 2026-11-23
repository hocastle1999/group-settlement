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
| 인증 | 회원가입 없이 그룹별 초대 링크 토큰 | 실제 정산 앱의 사용 방식. 1번 프로젝트의 JWT와 다른 설계 (3단계) |

자세한 내용은 [docs/design.md](docs/design.md).

## 로드맵

1. [x] **금액·원장 코어** (순수 파이썬): `Money`, 분할 규칙(균등·비율·정액), 이중기입 분개, 역분개, property-based 테스트
2. [ ] **최소 송금 알고리즘**: 그리디와 최적해 비교 (송금 횟수, 실행 시간)
3. [ ] **DB 스키마 + REST API**: MySQL, 추가만 하는 원장 테이블, CHECK 제약, 멱등성 키, 초대 링크 토큰
4. [ ] **동시성·정합성**: 동시 지출 추가와 정산 확정, 매번 합산 vs 잔액 테이블 비교
5. [ ] **다중 통화**: 지출 시점 환율 저장, 환산 반올림 처리
6. [ ] **정산 확정·송금 완료**: 감사 로그, 리포트 SQL, 데모 페이지
7. [ ] **배포**

## 실행 (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

## 사용 예 (1단계 코어)

```python
from app.ledger import EqualSplit, Ledger, Money, expense_entry, get_currency, transfer_entry

ledger = Ledger(get_currency("KRW"), ["민수", "지영", "현우"])
ledger.post(
    expense_entry("민수", Money.of(30_000, "KRW"), EqualSplit(["민수", "지영", "현우"]), "저녁")
)
{m: str(v) for m, v in ledger.balances().items()}
# {'민수': '20000 KRW', '지영': '-10000 KRW', '현우': '-10000 KRW'}  (합은 항상 0)

ledger.post(transfer_entry("지영", "민수", Money.of(10_000, "KRW")))
# 잔액: 민수 10000, 지영 0, 현우 -10000
```
