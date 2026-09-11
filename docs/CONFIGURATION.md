# 설정 안내

전체 배치 설정은 [config.toml.example](../config/config.toml.example)을 복사해 사용합니다.
샘플 NIM 분석만 실행한다면 [nvidia-nim.toml.example](../config/nvidia-nim.toml.example)이면
충분합니다. 알 수 없는 키, 중복 테이블, 허용 범위를 벗어난 값은 설정 오류입니다.

## 로그 입력 선택

`error_source.type`은 `file`, `oracle`, `elasticsearch` 중 하나입니다.
생략하면 기존 설정과 같이 Elasticsearch입니다. 0.4.0 폐쇄망 배포본은 `file` 예제를 기본 설정으로 복사합니다.
[파일 입력 안내](LOCAL_FILES.md)와 [file-onprem.toml.example](../config/file-onprem.toml.example)을 참고하세요.
`path`, `service`, `encoding`, `timestamp_timezone`을 설정하며 `service`는 `services`에 정의된 이름이어야 합니다.
`format` 기본값은 `standard`이며, `Server Instance`·`Exception Time`·`Exception StackTrace`로
구성된 내부 예외 보고서는 `format = "nexus"`로 원본을 직접 읽습니다.
기본 `filter_time_window = false`는 과거 파일까지 전체 조회합니다.

## LLM 선택

기존 설정과의 호환성을 위해 NVIDIA NIM과 온프레미스 모두 `[openai]` 테이블을 사용합니다.
중요망은 [onprem.toml.example](../config/onprem.toml.example)과
[압축 배포 안내](OFFLINE_DEPLOYMENT.md)를 사용하세요. `provider="onprem"`에는 내부
`base_url`을 반드시 명시해야 하며 기본 secret 이름은 `LLM_API_KEY`입니다.
Chat Completions를 호출하고 환경 변수 프록시를 사용하지 않습니다.

| 항목 | OpenAI | NVIDIA NIM |
|---|---|---|
| `provider` | `openai` (기본값) | `nvidia_nim` |
| 기본 `base_url` | `https://api.openai.com/v1` | `https://integrate.api.nvidia.com/v1` |
| 기본 `api_key_secret` | `OPENAI_API_KEY` | `NVIDIA_API_KEY` |
| HTTP API | Responses | Chat Completions |
| `model` | 계정에서 사용할 모델 ID를 지정 | 검증 모델 `nvidia/nemotron-3-super-120b-a12b` |

URL과 secret 이름은 생략했을 때만 공급자별 기본값이 적용됩니다. 공급자를 변경할 때는
기존 `base_url`, `api_key_secret`도 함께 변경하세요. 명시된 URL이 자동 교체되지는 않습니다.

| 공통 설정 | 생략 시 기본값 | 설명 |
|---|---|---|
| `timeout_seconds` | 60 | HTTP 요청 timeout; NIM 예시는 120 |
| `max_output_tokens` | 2000 | 출력 상한; NIM 예시는 4096, 모델 제한 확인 필요 |
| `structured_output` | `json_schema` | NIM/onprem은 `guided_json`, `json_object`도 명시적으로 선택 가능 |
| `enable_thinking` | 미지정 | NIM/onprem 서버의 지원 확인 후 설정 |
| `tls_ca` | 미지정 | LLM 사설 CA PEM; TLS 검증 비활성화는 지원하지 않음 |
| `auth_required` | true | onprem만 false 허용; 키가 존재하면 여전히 Bearer 인증 |
| `allow_http` | false | onprem만 true 허용; 내부 HTTP 연결을 명시적으로 선택 |

키를 TOML에 직접 넣는 `api_key` 필드는 지원하지 않습니다. 환경 변수 또는 credential
파일을 사용합니다. [NIM 상세 안내](NVIDIA_NIM.md)에서 출력 모드 차이를 확인하세요.

## Elasticsearch

`[error_source]`에 `url`과 `index`가 필요합니다. `name`은 체크포인트를 구분하는
이름으로, 같은 수집 대상을 운영하는 동안 유지하세요.

| 설정 | 기본값 | 설명 |
|---|---|---|
| `type` | `elasticsearch` | ES 선택; 생략 시 기존 ES 설정과 호환 |
| `name` | `elasticsearch` | SQLite에서 수집 대상을 구분하는 이름 |
| `tls_ca` | 미지정 | 사설 인증기관의 CA 인증서 경로 |
| `verify_tls` | true | false는 설정 검증에서 거부 |
| `request_timeout_seconds` | 30 | ES 요청 timeout |
| `api_key_secret` | `ES_API_KEY` | API key를 읽을 환경 변수·파일 이름 |
| `username_secret` / `password_secret` | `ES_USERNAME` / `ES_PASSWORD` | 기본 인증용 이름 |

HTTPS URL만 허용하며 URL에 사용자명·비밀번호·쿼리 문자열을 넣을 수 없습니다.
인증정보가 모두 없으면 무인증 HTTPS 연결을 시도합니다. ES 권한은 사용하는 인덱스의
조회·PIT 열기/닫기와 healthcheck가 가능해야 합니다.

## Oracle SQL

`error_source.type = "oracle"`이면 ES 설정 대신 `dsn`, `table`과
`[error_source.columns]` 매핑을 사용합니다. 전체 예시는
[oracle-onprem.toml.example](../config/oracle-onprem.toml.example)입니다.
ES의 `url`, `index`, `tls_ca` 등 전용 필드를 Oracle 설정에 섞으면 검증에서 거부합니다.
인증은 `ORACLE_USERNAME`·`ORACLE_PASSWORD` 환경 변수 또는 credential 파일을 사용합니다.
DATE/TIMESTAMP는 `timestamp_timezone`, 시간대 포함 TIMESTAMP는 `timestamp_type="timestamp_tz"`로
지정합니다. 필수 컬럼·TCPS wallet·조회 동작은 [Oracle 안내](ORACLE.md)를 따릅니다.

## 서비스와 Git

`[services.orders]`의 `orders`는 ES 로그의 `service.name` 또는 Oracle의 `service` 매핑 값과 일치해야 합니다.
서비스가 여러 개면 서비스마다 테이블을 추가합니다.

| 설정 | 기본값 / 필수 여부 | 설명 |
|---|---|---|
| `repository` | 필수 | 로컬 bare 또는 일반 Git 저장소 최상위 경로 |
| `application_packages` | 필수 | 내 애플리케이션 클래스 접두사 목록 |
| `source_roots` | `["src/main/java"]` | 저장소 기준 Java 소스 루트 목록 |
| `branch` | `HEAD` | 로그에 commit이 없을 때 사용할 로컬 ref |
| `diff_history` | 5 | 오류 파일의 최근 patch 이력 수; 1~50 |
| `framework_packages` | java, jdk, sun, org.springframework | 애플리케이션 소스 후보에서 제외할 접두사 |

원격 브랜치가 필요한 일반 clone에서는 `branch = "origin/main"`처럼 지정할 수 있습니다.
해당 ref는 로컬에 존재해야 합니다. 앱이 원격 저장소에 연결해 fetch하지는 않습니다.

- `git.commit.id`가 있으면 해당 commit에서 소스를 찾습니다. 로컬 ref에서 도달할 수 없는
  commit 또는 유효하지 않은 commit은 다른 브랜치로 대체하지 않습니다.
- commit이 없으면 해당 이벤트의 첫 소스 조회에서 선택한 ref를 SHA로 고정합니다.
  재시도에서도 같은 소스를 사용합니다.
- Git 소스는 작업 트리의 미커밋 변경이 아닌 commit의 내용을 읽습니다.

## 조회 범위와 동시 처리

| `[run]` 설정 | 기본값 | 설명 |
|---|---|---|
| `batch_size` | 500 | 페이지 크기; 한 실행의 총 오류 건수 제한은 아님 |
| `max_concurrency` | 3 | 동시 처리 이벤트 수 |
| `initial_lookback_minutes` | 60 | 체크포인트 없는 첫 실행의 조회 범위 |
| `ingestion_delay_seconds` | 60 | ES 수집 지연을 고려해 최신 구간 제외 |
| `overlap_minutes` | 5 | 마지막 체크포인트 이전부터 중복 조회 |
| `severities` | `["ERROR", "FATAL"]` | 조회할 로그 수준 |
| `lock_file` | `/run/log-analyzer/run.lock` | 중복 실행 방지 파일; 디렉터리 쓰기 권한 필요 |

마지막 체크포인트부터 다시 조회하므로 `initial_lookback_minutes`를 바꾸는 것만으로
이미 처리한 기간이 재분석되지는 않습니다. 새 조건을 실험할 때는 운영 상태 DB를
삭제하지 말고 별도의 `state.path`와 출력 디렉터리로 검증하세요.

## 분석 크기와 결과 보존

| 설정 | 기본값 | 설명 |
|---|---|---|
| `analysis.language` | `java` | 현재 유일한 전용 언어 |
| `analysis.prompt_version` | `java-incident-v2` | 현재 코드와 일치해야 함 |
| `analysis.analyzer_version` | `1` | 현재 코드와 일치해야 함 |
| `analysis.max_log_characters` | 100000 | 파싱할 로그 문자 상한 |
| `analysis.source_context_lines` | 30 | 오류 라인 앞뒤 최소 범위. 포함된 Java 메소드·생성자의 선언과 본문 전체까지 확장; 경계를 찾지 못하면 이 범위 사용. 전체 설정 예시는 75 |
| `analysis.max_source_bytes` | 256000 | 조회할 소스 파일 크기 상한 |
| `analysis.max_git_change_chars` | 30000 | Git 변경 이력 문자 상한 |
| `state.path` | `/var/lib/log-analyzer/state.db` | SQLite 상태 파일 |
| `state.busy_timeout_seconds` | 5 | SQLite 잠금 대기 |
| `report.directory` | `/var/lib/log-analyzer/reports` | Markdown 출력 |
| `report.retention_days` | 30 | 종료 작업·미사용 캐시와 참조 없는 리포트 보존 기간 |

전송 직전 정제 단계에서 메시지 4000자, stack trace 20000자, 소스 60000자,
변경 이력 30000자 상한이 추가로 적용됩니다. 원본 파일 전체는 보내지 않습니다.
소스 범위에는 오류가 발생한 메소드 전체를 포함합니다. 정제한 소스가 60000자를 넘거나
요청 생성 시 100000자를 넘으면 메소드를 잘라 보내지 않고 해당 분석을 실패로 기록합니다.
메소드 경계를 확인할 수 없는 초기화 블록·불완전한 소스 등에는 오류 줄 앞뒤 범위를 사용합니다.
메소드 전체 분석은 이전 부분 소스 분석 캐시와 구분합니다. 이미 완료된 이벤트와 저장된
재시도 요청을 이번 변경으로 자동 재수집하거나 재작성하지는 않습니다.

보존 정리는 완전한 수집 구간을 처리한 실행에서 수행됩니다. 재시도 중 작업과 아직
참조되는 리포트는 단순 파일 나이만으로 삭제하지 않습니다. 종료 작업의 `updated_at`,
캐시의 `last_used_at`을 기준으로 정리합니다.

## 경로와 비밀정보

상대 경로는 프로세스 실행 디렉터리 기준입니다. systemd의 작업 디렉터리는
`/opt/log-analyzer`이므로 운영 설정에는 절대 경로를 사용하세요.

환경 변수의 값이 같은 이름의 credential 파일보다 우선합니다. 직접 실행할 때는
`CREDENTIALS_DIRECTORY`로 파일이 있는 디렉터리를 지정하고, systemd에서는
`LoadCredential`을 사용합니다. 로컬 `config/*.toml`, `config/credentials/`, `repos/`,
`state/`, `reports/`는 Git 제외 대상입니다.
