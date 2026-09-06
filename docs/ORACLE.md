# Oracle SQL로 오류 로그 조회하기

`0.3.0`부터 `[error_source] type = "oracle"`로 Oracle 테이블 또는 뷰를 조회합니다.
하나의 설정에서 ES 또는 Oracle 중 하나를 선택합니다. 조회 이후 Java 오류 파싱,
Git 소스 확인, 내부 LLM 분석, SQLite 중복 제거·재시도와 Markdown 리포트는 동일합니다.

테이블과 Oracle 버전이 아직 정해지지 않아 **예시 매핑을 제공한 상태**입니다.
실제 접속 주소·컬럼·권한·시간대를 반영한 뒤 `healthcheck`와 소량 배치를 확인하세요.

## 1. 준비할 정보

| 항목 | 예시·조건 |
|---|---|
| DB | Oracle 12.1 이상, python-oracledb Thin 연결을 지원하는 서버 설정 |
| 접속 주소 | `tcp://oracle.internal:1521/LOGPDB`; TLS는 `tcps://oracle.internal:2484/LOGPDB` |
| 계정 | 접속 권한과 대상 테이블·뷰의 SELECT 권한을 가진 조회 계정 |
| 테이블 또는 뷰 | `APP_ERROR_LOG` 또는 `LOG_OWNER.APP_ERROR_LOG` |
| 이벤트 ID | 변경·재사용하지 않는 고유 ID, 문자열 또는 정수 NUMBER |
| 발생 시각 | DATE / TIMESTAMP 또는 TIMESTAMP WITH TIME ZONE |
| 서비스 | `[services.서비스명]`과 일치하는 문자열 |
| 메시지·trace | VARCHAR2, CLOB 또는 NCLOB |

Oracle Client/Instant Client 없이 비동기 **Thin 모드**로 연결합니다.
Oracle 11g, Thick 모드, Native Network Encryption 전용 구성은 이번 어댑터 범위에
포함하지 않습니다. TLS 구성과 Thin 호환성은 실제 DB 환경에서 확인해야 합니다.
관련 기준은 [드라이버 소개](https://python-oracledb.readthedocs.io/en/stable/user_guide/introduction.html)와
[연결 문서](https://python-oracledb.readthedocs.io/en/stable/user_guide/connection_handling.html)를 따릅니다.

## 2. 설정 파일 만들기

폐쇄망 배포본의 압축을 푼 디렉터리에서 실행합니다. 기존 운영 설정이 있다면 먼저 보관합니다.

```bash
cp config/oracle-onprem.toml.example config/config.toml
```

전체 예시는 [oracle-onprem.toml.example](../config/oracle-onprem.toml.example)입니다.
주요 부분을 실제 환경에 맞게 수정합니다.

```toml
[error_source]
type = "oracle"
name = "oracle-production-logs"
dsn = "tcp://oracle.internal:1521/LOGPDB"
table = "LOG_OWNER.APP_ERROR_LOG"
timestamp_type = "timestamp"
timestamp_timezone = "+09:00"
request_timeout_seconds = 30
username_secret = "ORACLE_USERNAME"
password_secret = "ORACLE_PASSWORD"

[error_source.columns]
event_id = "EVENT_ID"
occurred_at = "OCCURRED_AT"
service = "SERVICE_NAME"
severity = "LOG_LEVEL"
message = "ERROR_MESSAGE"
stack_trace = "STACK_TRACE"
```

`name`은 SQLite의 체크포인트·중복 판정에 사용하는 수집원 이름입니다.
기존 ES에서 전환할 때는 다른 이름을 사용합니다. ES와 Oracle 사이에서 동일 사건인지
자동으로 판별하지 않습니다. 같은 테이블을 계속 조회할 때는 이름을 유지합니다.

| 매핑 키 | 필수 | 설명 |
|---|---|---|
| `event_id` | 예 | 유일한 ID; NUMBER는 정수만 허용, 38자리 ID도 정밀도 보존 |
| `occurred_at` | 예 | 발생 시각; 문자열이면 DB 뷰에서 명시적으로 날짜 타입 변환 |
| `service` | 예 | 예: `order_api` → `[services.order_api]` |
| `severity` | 예 | 배치는 정확히 `ERROR`, `FATAL` 값을 조회하며 대소문자를 구분 |
| `message` | 예 | 오류 메시지; NULL 행은 매핑 오류로 격리 |
| `stack_trace` | 아니오 | 기본 `STACK_TRACE`; 컬럼이 없으면 `stack_trace = ""` |
| `error_type` | 아니오 | 예외 클래스; 생략하면 NULL |
| `environment`, `version` | 아니오 | 실행 환경·서비스 버전; 생략하면 NULL |
| `git_commit` | 아니오 | 오류 당시 commit; 없으면 설정된 로컬 Git ref를 사용 |
| `trace_id` | 아니오 | 요청 추적 ID; 생략하면 NULL |

테이블명은 `테이블` 또는 `스키마.테이블`, 컬럼명은 일반 Oracle 식별자만 허용합니다.
따옴표가 필요한 이름, DB link, SQL 표현식·자유 SQL은 설정하지 않습니다.
조인, 시스템별 필터, 숫자 로그 수준 변환, 복합 ID가 필요하면 DBA가 **조회용 뷰**로
정규화하고 해당 뷰를 설정합니다. 뷰도 한 행당 ID가 유일해야 합니다.

[테이블·인덱스 예시](../deploy/oracle-example.sql)는 DBA 검토용입니다.
프로그램은 테이블을 생성하거나 원본 로그를 수정·삭제하지 않습니다.

## 3. 발생 시각 맞추기

- DATE / TIMESTAMP: `timestamp_type = "timestamp"`와 실제 저장 기준의
  `timestamp_timezone`을 지정합니다. 한국 시각 저장은 `+09:00`, UTC 저장은 `+00:00`입니다.
- TIMESTAMP WITH TIME ZONE: `timestamp_type = "timestamp_tz"`를 사용합니다.
  SQL의 `SYS_EXTRACT_UTC`로 읽고 조회 조건에도 UTC 오프셋을 포함합니다.
  이 모드에서는 `timestamp_timezone`이 결과에 영향을 주지 않습니다.

DB의 문자열 날짜 형식이나 세션 NLS 설정에 의존하지 않도록 조회 경계를 명시적
형식의 `TO_TIMESTAMP` / `TO_TIMESTAMP_TZ` 바인드로 전달합니다.
리포트·체크포인트에는 UTC로 정규화합니다. 지역 이름·일광절약시간과
TIMESTAMP WITH LOCAL TIME ZONE은 이번 설정의 지원 대상이 아닙니다.

## 4. 비밀번호와 TLS 준비

계정·비밀번호를 TOML이나 DSN에 넣지 않습니다. 배포본의 인증 파일을 사용합니다.

```bash
chmod 700 config/credentials
read -rp 'Oracle username: ' ORACLE_USER_INPUT
read -rsp 'Oracle password: ' ORACLE_PASSWORD_INPUT; printf '\n'
(umask 077; printf '%s' "$ORACLE_USER_INPUT" > config/credentials/ORACLE_USERNAME)
(umask 077; printf '%s' "$ORACLE_PASSWORD_INPUT" > config/credentials/ORACLE_PASSWORD)
unset ORACLE_USER_INPUT ORACLE_PASSWORD_INPUT
```

같은 이름의 환경 변수가 있으면 파일보다 우선합니다. 일반 Python CLI에서는
`CREDENTIALS_DIRECTORY`를 인증 파일 디렉터리로 지정합니다. 폐쇄망 실행기는
이를 `config/credentials`로 설정합니다. systemd는 `LoadCredential=ORACLE_USERNAME:...`과
`LoadCredential=ORACLE_PASSWORD:...`를 추가할 수 있습니다.

TCPS는 `dsn`의 프로토콜과 포트를 바꿉니다. 드라이버 연결에 서버 이름 검증을 요청하며,
DBA가 제공한 PEM wallet이 필요하면 다음 설정을 `[error_source]`에 추가합니다.

```toml
wallet_location = "config/oracle-wallet"
wallet_password_secret = "ORACLE_WALLET_PASSWORD"
```

암호화된 wallet이면 `config/credentials/ORACLE_WALLET_PASSWORD`에 암호를 저장합니다.
`config/oracle-wallet/`은 Git·컨테이너 빌드 전송에서 제외하며 배포본에도 자동 포함하지 않습니다.
LLM의 `tls_ca`는 Oracle에 적용되지 않습니다. Wallet 파일·CA·서버 인증 방식은
[Oracle TLS 연결 안내](https://python-oracledb.readthedocs.io/en/stable/user_guide/connection_handling.html#connecting-to-oracle-cloud-autonomous-databases)를 참고해 실제 환경에 맞게 준비합니다.

## 5. 확인하고 실행하기

Git 저장소와 `[openai]`의 내부 LLM 주소·모델·인증을 설정한 뒤 실행합니다.

```bash
./log-analyzer doctor
./log-analyzer check-config
./log-analyzer healthcheck
./log-analyzer run
```

`doctor`는 동봉 Oracle 드라이버를 포함해 런타임을 검사하고 네트워크를 사용하지 않습니다.
`healthcheck`는 Oracle 연결·매핑 컬럼을 `SELECT ... WHERE 1 = 0`으로 검사하며,
Git·SQLite·LLM 모델 목록도 확인합니다. 실제 로그는 `run`에서 읽습니다.
원본에서 읽은 메시지·CLOB 길이를 제한한 후 기존 정제 과정을 거쳐 LLM에 전달합니다.

소스 설치 환경의 명령은 다음과 같습니다.

```bash
python -m log_analyzer healthcheck --config config/config.toml
python -m log_analyzer run --config config/config.toml
```

## 조회·중복·실패 처리

배치 시작 때 조회 구간을 고정하고 발생 시각과 로그 수준을 **바인드 변수**로 전달합니다.
조건은 `발생시각 >= 시작 AND 발생시각 <= 종료`이며 발생시각·ID 순으로 정렬합니다.
한 번 실행한 SELECT 커서에서 `run.batch_size`행씩 가져옵니다. batch_size는 전체 조회 건수
상한이 아닙니다. 마지막까지 읽어야 종료 시각을 체크포인트에 저장합니다.

한 SELECT의 읽기 일관성을 유지하기 위해 LLM 처리 중에도 커서를 유지합니다.
조회 이후에 커밋된 행은 현재 결과에 추가되지 않습니다. 다음 배치에서 다시 조회하는
overlap 구간에 속하면 처리됩니다. 장기 실행 시 UNDO 보존 기간·idle session 제한 때문에
조회가 실패할 수 있으므로 DBA와 함께 조회 구간, 인덱스, 배치 처리 시간을 확인합니다.
`request_timeout_seconds`는 연결 및 DB round trip 제한이며 전체 배치의 시간 제한은 아닙니다.

조회·LOB 읽기·연결이 실패하면 체크포인트를 진행하지 않습니다. 다음 실행에서 같은 구간을
재조회하되 SQLite에 완료된 ID는 건너뜁니다. 안정적인 ID가 없는 행은 배치를 중단하며,
ID는 있지만 나머지 필드가 잘못된 행은 영구 매핑 오류로 기록합니다.
해당 ID는 설정을 고쳐도 자동 재분석하지 않으므로 첫 운영 전 소량 데이터로 매핑을 확인합니다.

로그 테이블은 추가 기록 방식으로 운영하고 ID를 재사용하지 않아야 합니다.
오래 지연되어 overlap보다 과거 시각으로 들어온 행, 과거 행 수정, 이미 처리한 ID의
내용 교체는 자동 발견·재분석 대상이 아닙니다. 상세 공통 동작은
[중복·재발 처리](DUPLICATE_ERRORS.md)를 참고하세요.

## 검증 범위와 실제 DB 테스트

단위 테스트는 SQL 바인딩·시간대·CLOB 제한·페이지 조회·매핑 오류를 확인합니다.
모의 DB 커서와 실제 SQLite·리포트 작성기를 연결한 테스트로 중간 실패 후 복구,
중복 건너뛰기와 동일 오류의 분석 캐시 재사용도 확인합니다.
**실제 Oracle DB 연결·SQL 실행·TCPS는 접속 정보가 없어 아직 검증하지 않았습니다.**

DB가 준비되면 별도 시험 테이블에 최근 24시간 이내 ERROR 로그를 3건 이상 200건 미만
준비합니다. 예시 DDL의 컬럼과 설정 매핑을 맞추고 같은 시각의 서로 다른 ID, 한글 CLOB을
포함하세요. 비운영 시험 설정 파일을 지정해 읽기 전용 통합 테스트를 실행합니다.

```bash
export CREDENTIALS_DIRECTORY="$PWD/config/credentials"
export LOG_ANALYZER_TEST_ORACLE_CONFIG="$PWD/config/oracle-test.toml"
python -m unittest discover -s tests -p test_oracle_live.py -v
```

이 테스트는 SELECT만 실행하며 LLM을 호출하거나 DB fixture를 만들고 삭제하지 않습니다.
일반 개발 환경에서는 접속 설정이 없으면 제외됩니다. 실제 DB 버전·매핑이 확정되면
이 테스트와 소량 `run` 결과를 운영 전 확인 기록에 추가합니다.
