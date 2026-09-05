# 내 Java 오류 로그 분석하기

[README](../README.md)의 샘플 분석을 마쳤다면 실제 서비스 로그를 연결할 수 있습니다.
이 안내는 Linux/WSL에서 한 번 실행하는 방법입니다. 서버에 설치해 자동 실행하려면
[배포 안내](../deploy/README.md)를 따르세요.

## 1. 로그 형태 확인

현재 입력은 Elasticsearch입니다. 하나의 오류와 전체 stack trace를 하나의 ES 문서에
저장해야 합니다. 다음은 `orders` 서비스의 로그 형태 예시이며 Git commit은 생략했습니다.

```json
{
  "@timestamp": "2026-09-05T06:00:00Z",
  "event": {"id": "order-error-001"},
  "log": {"level": "ERROR"},
  "service": {"name": "orders", "environment": "test"},
  "error": {
    "type": "java.lang.NullPointerException",
    "message": "Cannot invoke String.trim() because customer is null",
    "stack_trace": "java.lang.NullPointerException: customer is null\n\tat com.example.order.OrderService.customerName(OrderService.java:4)"
  }
}
```

위 시각은 형식 설명용입니다. 테스트 데이터를 직접 넣는다면 현재 조회 구간에 속하는
시각을 사용하세요. 기본 첫 조회는 최근 60분이며 마지막 60초는 제외합니다.

| 값 | 확인할 내용 |
|---|---|
| `@timestamp` | 실제 오류 발생 시각, 가능하면 시간대 포함 |
| `log.level` | 기본 조회 조건은 `ERROR` 또는 `FATAL` |
| `service.name` | 설정 테이블 이름과 같아야 함 |
| `error.message` 또는 `message` | 오류 메시지 |
| `error.stack_trace` 또는 `message` | 클래스·파일·라인을 포함한 전체 Java stack trace |
| `event.id` | 권장; 없으면 ES의 인덱스명과 `_id`로 식별 |
| `git.commit.id` | 선택; 실제 배포 commit을 알고 있을 때만 제공 |

여러 문서로 흩어진 stack trace는 자동으로 합치지 않습니다. Filebeat 등 수집기의
multiline 설정으로 먼저 결합하세요. 현재 필드명은 어댑터에 고정되어 있으며 TOML에서
임의 필드명으로 바꾸는 mapping 옵션은 없습니다.

## 2. 소스 저장소 준비

오류가 발생한 **애플리케이션의 Git 저장소**가 필요합니다. 이 도구 자체의 저장소와는
다릅니다. 아래 URL은 내 서비스 저장소의 실제 주소로 바꿉니다.

```bash
mkdir -p repos
git clone --mirror https://github.com/YOUR_ORG/orders.git repos/orders.git
git --git-dir=repos/orders.git rev-parse main
```

`main`이 없는 저장소는 실제 브랜치명으로 바꾸세요. 일반 clone도 지원하지만 설정의
`repository`는 저장소 최상위 경로여야 합니다. 프로그램은 checkout이나 fetch를 하지
않습니다. 소스 갱신은 운영자가 별도로 수행합니다.

예외의 클래스가 `com.example.order.OrderService`이고 Java 파일이
`src/main/java/com/example/order/OrderService.java`에 있다면:

```toml
[services.orders]
repository = "repos/orders.git"
branch = "main"
source_roots = ["src/main/java"]
application_packages = ["com.example.order"]
```

멀티 모듈 프로젝트는 `source_roots = ["orders/src/main/java", "common/src/main/java"]`
처럼 실제 모듈 경로를 지정합니다. 파일의 package 선언과 오류 라인도 일치해야 합니다.
commit 없는 로그의 라인 번호는 현재 소스와 다를 수 있습니다. 확인된 파일·라인이
실제 배포 소스와 일치하는지는 리포트 검토 때 확인하세요.

## 3. 전체 설정 만들기

저장소 루트에서:

```bash
cp config/config.toml.example config/local.toml
mkdir -p state reports
```

`config/local.toml`을 편집합니다. 아래 항목은 이미 있는 테이블의 값을 바꾸는
예시입니다. 같은 테이블을 파일 끝에 다시 추가하면 TOML 중복 오류가 납니다.

```toml
[run]
lock_file = "state/run.lock"

[error_source]
name = "my-logs"
url = "https://my-elasticsearch.example.com:9200"
index = "logs-*"

[services.orders]
repository = "repos/orders.git"
branch = "main"
diff_history = 5
source_roots = ["src/main/java"]
application_packages = ["com.example.order"]

[openai]
provider = "nvidia_nim"
base_url = "https://integrate.api.nvidia.com/v1"
model = "nvidia/nemotron-3-super-120b-a12b"
api_key_secret = "NVIDIA_API_KEY"
timeout_seconds = 120
max_output_tokens = 4096
structured_output = "json_schema"
enable_thinking = false

[state]
path = "state/state.db"

[report]
directory = "reports"
retention_days = 30
```

기존 `[services.order_api]`는 내 로그의 이름인 `[services.orders]`로 변경합니다.
설정 예시의 `tls_ca` 경로도 실제 내부 CA 파일로 바꾸세요. 공인 CA를 사용한다면
`tls_ca` 줄을 지울 수 있습니다. HTTP 주소와 TLS 검증 비활성화는 허용하지 않습니다.

상대 경로는 **설정 파일이 아닌 실행 디렉터리 기준**입니다. 이 안내의 명령은 모두
저장소 루트에서 실행합니다. 운영 환경에서는 절대 경로를 권장합니다.

## 4. 인증정보 준비

NIM 키는 빠른 시작에서 만든 `config/credentials/NVIDIA_API_KEY`를 사용합니다.
ES API 키도 `config/credentials/ES_API_KEY` 파일에 키 값만 저장합니다.
기본 인증을 사용하는 ES라면 대신 `ES_USERNAME`, `ES_PASSWORD` 두 파일을 만듭니다.

```bash
export CREDENTIALS_DIRECTORY="$PWD/config/credentials"
chmod 700 config/credentials
chmod 600 config/credentials/*
```

환경 변수로 같은 이름을 지정해도 됩니다. ES API 키는 기본 인증보다 우선하며,
비밀번호 없이 사용자명만 지정하면 설정 오류로 종료합니다. 비밀값을 TOML에 넣거나
Git에 추가하지 마세요.

## 5. 연결 확인 후 한 번 분석

```bash
.venv/bin/python -m log_analyzer healthcheck --config config/local.toml
.venv/bin/python -m log_analyzer run --config config/local.toml
```

`healthcheck_succeeded`를 확인한 뒤 `run`을 실행합니다. NIM healthcheck의 모델 조회는
실제 생성 권한을 보장하지 않으므로 첫 NIM 연결은 README의 샘플 분석도 확인하세요.
`run_skipped_locked`는 이미 실행 중이라는 뜻이며 연결 검사 성공 메시지가 아닙니다.

한 실행은 고정된 시간 구간의 오류를 처리하고 종료합니다. `run_completed`의
`summary.completed`, `no_source`, `cache_hits`, `outstanding_retries`와
`checkpoint_saved`를 확인하세요. 같은 이벤트를 다시 읽어도 중복 처리하지 않고,
같은 오류·소스·분석 설정의 결과는 캐시를 재사용합니다.

## 6. 결과 읽기

`reports/`의 Markdown 파일을 엽니다. [샘플 리포트](examples/nim-report.md)와 같은
형태이며 다음 순서로 검토하면 됩니다.

1. **Status / Error**: 분석 완료 여부와 실제 오류
2. **Confirmed source location**: 참조한 파일·라인
3. **Source revision basis**: 배포 commit인지, 설정 ref의 소스인지
4. **Evidence-based root causes**: 근거가 붙은 원인 후보
5. **Recommended fixes / Validation**: 수정안과 재현·회귀 테스트
6. **Unknowns**: 추가로 확인해야 할 조건

`NO_SOURCE` 리포트는 소스를 확인하지 못한 결과이며 LLM을 호출하지 않았습니다.
LLM의 confidence는 모델이 표현한 확신 정도로, 장애 원인일 실제 확률은 아닙니다.
발생 횟수는 작성 시점의 보존된 작업 수이고, 기존 리포트가 실시간 갱신되지는 않습니다.
리포트 이후 같은 오류가 새 이벤트로 발생하면 분석 캐시를 재사용할 수 있지만 리포트는
새로 생성합니다. First seen / Last seen은 해당 이벤트 시각이며 오류 그룹의 집계 시각이
아닙니다. 자세한 기준과 사례는 [중복 오류와 재발 처리](DUPLICATE_ERRORS.md)를 참고하세요.

## 7. 주기 실행과 다음 단계

[배포 안내](../deploy/README.md)에 따라 systemd timer를 설치하면 기본 10분마다
실행됩니다. 실행 중 문제가 생기면 [문제 해결](TROUBLESHOOTING.md)을 확인하세요.
전체 설정값은 [설정 안내](CONFIGURATION.md)에 정리되어 있습니다.
