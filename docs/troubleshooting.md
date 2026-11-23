# 문제 해결 기록

실제로 겪은 문제를 문제, 원인, 해결, 배운 점 순서로 남긴다.

## 마이그레이션 downgrade가 "Cannot drop index ... needed in a foreign key constraint"로 실패 (3단계)

**문제**: 첫 마이그레이션(테이블 5개 생성)을 개발 DB에 올린 뒤 `alembic downgrade base`로 왕복을 확인하자 종료 코드 1이 나왔다.
`alembic current`는 여전히 head였다.

```
pymysql.err.OperationalError: (1553, "Cannot drop index 'ix_idempotency_keys_group_id_entry_id':
needed in a foreign key constraint")
```

**원인**: downgrade가 autogenerate의 모양을 따라 `drop_index`를 하고 나서 `drop_table`을 했다.
`ix_idempotency_keys_group_id_entry_id`는 복합 외래 키 `(group_id, entry_id) -> journal_entries`가 쓰는 인덱스다.
InnoDB는 외래 키에 쓸 인덱스가 반드시 있어야 하므로, 테이블이 남아 있는 동안 그 인덱스만 지우는 것을 거부한다.
(기본키 앞부분이 외래 키 열과 같으면 기본키가 인덱스 역할을 하지만, 이 테이블의 기본키는 `(group_id, idem_key)`라 따로 인덱스가 필요했다.)

**확인**: MySQL DDL은 트랜잭션으로 되돌릴 수 없어서, 실패하면 반쯤 적용된 상태가 남을 수 있다(smart-elevator에서 겪은 문제).
`alembic current`와 실제 테이블, 인덱스를 먼저 확인했다. 첫 문장에서 실패했기 때문에 지워진 것은 없었다.

**해결**: downgrade에서 `drop_index`를 빼고 `drop_table`만 한다. 테이블을 지우면 그 테이블의 인덱스도 함께 지워진다.
고친 뒤 개발 DB에서 head -> base -> head 왕복과 `alembic check`를 다시 확인했다. 테스트도 세션마다 base -> head로 왕복한다.

**배운 점**
- 외래 키 열에 직접 만든 인덱스는 외래 키와 묶여 있다. 지우려면 외래 키를 먼저 지우거나 테이블째 지운다.
- 테스트 DB가 처음 비어 있을 때는 downgrade가 할 일이 없어 이 오류가 드러나지 않는다. 테이블이 있는 개발 DB에서 직접 왕복해 봐야 했다.

## 균등 분할의 남는 1원이 늘 같은 사람에게 간다 (3단계)

**문제**: API 테스트에서 민수가 10,000원을 내고 3명이 나누면 민수 잔액이 +6,667일 것이라고 적었는데, 실제로는 +6,666이었다.

**원인**: 남는 1원은 최대 나머지 방식으로 나누고, 나머지가 같으면 멤버 ID 순서로 준다(결정성, 1단계 설계).
균등 분할은 모두 나머지가 같으므로 **ID가 가장 앞선 멤버**가 3,334원을 낸다. 민수는 가장 먼저 등록돼 ID가 가장 작았다.
테스트를 쓰면서 "결제한 사람이 남는 돈을 덜 낼 것"이라고 막연히 생각했다.

**해결**: 테스트 기대값을 규칙대로 고쳤다(민수가 3,334원 부담, 잔액 +6,666). USD 테스트의 1센트도 같은 이유였다.

**배운 점 / 남은 문제**
- 규칙은 결정적이지만, 지출이 쌓이면 같은 사람이 매번 1원씩 더 낸다. 금액은 작아도 "늘 같은 사람"이라는 편향이다.
- 개선 후보: 남는 단위를 받을 순서를 분개 id로 돌리기(라운드 로빈), 또는 지금까지 남는 단위를 덜 받은 사람부터 주기.
  어느 쪽이든 결정성은 유지된다. 5단계(환산 반올림)에서 반올림 규칙을 다시 정할 때 함께 다룬다.

## PowerShell로 API를 부르면 한글이 `??`로 저장되거나 깨져 보인다 (3단계)

**문제**: README 사용 예를 Windows PowerShell 5.1의 `Invoke-RestMethod`로 실행했더니
첫 멤버 "민수"가 `??`로 저장됐고, 두 번째 "지영"도 `??`가 되어 이름 중복(409)이 났다.
`charset=utf-8`을 붙인 뒤에는 저장은 됐지만 응답의 이름이 `ë¯¼ì` 같은 글자로 보였다.

**원인**: 문제가 두 개 겹쳐 있었다. DB에 저장된 바이트를 직접 확인해 나눴다.
1. **요청**: `-ContentType "application/json"`처럼 charset이 없으면 PowerShell 5.1은 문자열 본문을
   ISO-8859-1로 인코딩한다. 한글은 이 문자 집합에 없으므로 `?`(0x3F)가 된다. DB에는 `3f3f`가 들어 있었다.
2. **응답**: 서버는 UTF-8로 보냈지만(`ebafbcec8898` = "민수") Content-Type이 charset 없는 `application/json`이었다.
   JSON은 표준(RFC 8259)상 UTF-8뿐이라 Starlette는 charset을 붙이지 않는데, PowerShell 5.1은 charset이 없으면
   ISO-8859-1로 읽는다.

**해결**
- 요청: README 예시의 모든 요청에 `-ContentType "application/json; charset=utf-8"`.
- 응답: 미들웨어(`app/main.py`의 `json_charset_utf8`)가 `application/json`에 `; charset=utf-8`을 붙인다.
  응답 클래스를 바꾸는 방법도 있지만, 그러면 FastAPI가 만드는 오류 응답(422 검증 오류, 401, 404)에는 적용되지 않아 미들웨어로 했다.
  테스트(`test_json_responses_declare_utf8`)가 성공 응답과 오류 응답 모두 확인한다.

**배운 점**
- 한글이 깨지면 "어디서" 깨졌는지 저장된 바이트(hex)로 먼저 나눈다. 보낼 때 깨진 것(`3f`)과 읽을 때 깨진 것(올바른 UTF-8을 잘못 해석)은 고치는 곳이 다르다.
- 표준상 필요 없는 값이라도 널리 쓰이는 클라이언트가 그 값에 기대면 붙이는 것이 실용적이다. charset을 붙여도 다른 클라이언트에는 해가 없다.
- TestClient(httpx)는 UTF-8로 주고받으므로 테스트로는 드러나지 않았다. README 예시를 실제 서버에서 그대로 실행해 보고 알았다.

## DB 테스트가 17초에서 70 ~ 100초로 느려졌다 (3단계)

**문제**: 테스트 수는 거의 같은데 전체 실행이 17초에서 70 ~ 100초가 됐다. `--durations`를 보면 모든 DB 테스트의 setup이 1.6 ~ 3.7초였다.

**원인**: 테스트마다 테이블 5개를 `TRUNCATE`로 비우고 있었다. InnoDB의 TRUNCATE는 행을 지우는 것이 아니라 테이블 파일(.ibd)을 지우고 새로 만든다(DDL).
파일 작업이라 디스크 상황(백신 검사 등)에 따라 느려진다. 빈 테이블 5개에 약 1.1초가 걸렸다. 처음에 빨랐던 이유는 확실히 알지 못한다.

| 방법 | 빈 테이블 5개 정리 |
| --- | --- |
| `TRUNCATE TABLE` | 1,004 ~ 1,259ms |
| `DELETE FROM` | 2 ~ 5ms |

**해결**: 정리를 `DELETE FROM`으로 바꿨다(외래 키 검사는 그 연결에서만 잠시 끈다). 전체 테스트는 13 ~ 20초로 돌아왔다.
DELETE는 AUTO_INCREMENT를 되돌리지 않으므로, "없는 분개 id"로 999 같은 작은 값을 쓰던 테스트는 절대 생길 수 없는 큰 값(10^15)으로 바꿨다.

**배운 점**
- 행이 몇 개 없는 테이블은 DELETE가 훨씬 싸다. TRUNCATE는 큰 테이블을 통째로 비울 때 유리하다(행마다 언두 로그를 남기지 않는다).
- 테스트가 느려지면 `--durations`로 call과 setup 중 어디가 느린지부터 본다.
