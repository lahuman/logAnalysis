# 내 Java 오류 로그 분석하기

실행마다 설정 파일을 주입해 여러 로그와 Git 저장소를 처리하려면
[실행별 설정 안내](RUN_PROFILES.md)를 참고하세요.

로컬 텍스트 파일로 시작하려면 [파일 입력 안내](LOCAL_FILES.md)를 따르세요.
0.5.0 폐쇄망 배포본의 기본 입력입니다. 아래 ES·Oracle 연결 안내는 해당 입력 선택 시 적용합니다.

[README](../README.md)의 샘플 분석을 마쳤다면 실제 서비스 로그를 연결할 수 있습니다.
이 안내는 Linux/WSL에서 한 번 실행하는 방법입니다. 서버에 설치해 자동 실행하려면
[배포 안내](../deploy/README.md)를 따르세요.

## 1. 로그 형태 확인

아래는 Elasticsearch 입력 기준입니다. Oracle 테이블·뷰의 오류를 읽으려면
[Oracle SQL 연결 안내](ORACLE.md)를 먼저 따르세요. 이후 소스 조회·분석·리포트는 동일합니다. 하나의 오류와 전체 stack trace를 하나의 ES 문서에
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

`reports/`의 Markdown 파일을 엽니다. [한글 샘플 리포트](examples/error-priority-report.md)와 같은
형태이며 다음 순서로 검토하면 됩니다.

1. **오류 수준 및 대응 우선순위**: 높음·중간·낮음, 권장 처리 시점, 판단 근거와 영향, 대응 방법, 상향 조건
2. **처리 상태 / 오류 정보**: 분석 완료 여부와 실제 오류
3. **확인된 소스 위치**: 참조한 파일·라인
4. **소스 버전 기준**: 배포 커밋인지, 설정 ref의 소스인지
5. **근거에 따른 원인 후보**: 근거가 붙은 원인 후보
6. **권장 수정 방법 / 검증 및 회귀 테스트**: 수정안과 재현·회귀 테스트
7. **추가 확인 사항 / 세 줄 요약**: 미확인 조건과 판단·영향·대응의 핵심 요약

기본 리포트의 제목·안내 문구와 LLM 분석 설명은 한글로 출력합니다. 파일은 UTF-8로 저장하며,
원문 메시지·스택 트레이스·파일명·코드는 검색과 근거 확인을 위해 원문을 유지합니다.
각 리포트 끝에는 판단·영향·대응을 각각 한 줄로 정리한 세 줄 요약을 붙입니다.
이전 언어 정책으로 생성한 분석 캐시는 새 분석에 사용하지 않습니다.

`run`은 모든 리포트 처리와 자원 정리를 마친 뒤 표준 출력(stdout)에 UTF-8 한글 세 줄 요약을 출력합니다.
기존 JSON 진단 로그는 표준 오류(stderr)에 계속 출력합니다. 예시는 다음과 같습니다.

```text
처리 완료: 리포트 4건(분석 완료 3건, 소스 미확인 1건), 중복 확인 2건.
오류 수준: 높음 1건 · 중간 2건 · 낮음 1건(소스 미확인은 중간·잠정 판단). 높음 오류부터 즉시 대응하세요.
남은 작업: 재시도 대기 0건 · 실패 0건 · 정리 실패 0건. 결과: reports
```

수준별 건수는 이번 실행에서 생성한 리포트 기준이며 캐시를 사용해 새로 작성한 리포트도 포함합니다.
소스 미확인은 중간·잠정 판단으로 집계합니다. 중복 확인은 이미 등록된 이벤트를 다시 읽은 횟수로,
미완료 작업의 재개도 포함할 수 있습니다. 처리 중단 시 첫 줄을 `처리 중단`으로 표시하고,
재시도 대기·실패·정리 실패 건수를 함께 보여 줍니다. 리포트가 없어도 세 줄 요약을 출력합니다.

| 오류 수준 | 분류 기준 | 권장 처리 시점과 방법 |
|---|---|---|
| **높음** | 서비스 중단, 핵심 기능의 지속 실패, 데이터 손상·유실 또는 보안 영향의 근거가 있음 | 즉시 영향 확인·대응에 착수. 담당자 공유, 피해 확산 방지와 복구를 우선하고 원인 수정·회귀 검증 수행 |
| **중간** | 기능 실패가 있으나 범위가 제한적이거나 아직 확인되지 않음 | 당일 업무 시간 내 영향 점검, 임시 대응과 수정 일정 확정. 영향이 지속·확대되면 즉시 상향 |
| **낮음** | 영향이 격리되고 복구 가능하며 서비스·데이터 영향이 경미하다는 근거가 있음 | 모니터링하며 다음 정기 개선 일정에 반영. 재현·회귀 테스트를 포함하고 반복 증가나 사용자 영향 발생 시 재평가 |

처리 시점은 기본 권장 기준이며 조직의 운영 기준과 실제 영향에 따라 결정합니다.
오류 수준은 원본 로그의 ERROR·FATAL과 별도로 판단하며, 수정안의 `risk`는 코드 변경 위험이므로
오류의 긴급도와 같지 않습니다. 예외 이름이나 발생 횟수만으로 낮음으로 분류하지 않습니다.
각 리포트에는 해당 오류의 판단 근거·영향·우선 대응·상향 조건을 함께 표시합니다.
영향 판단 근거가 부족하거나 소스가 없으면 **중간·잠정 판단**으로 표시합니다.
잠정 분류는 실제 피해가 중간이라는 확정이 아니므로, 서비스 상태와 데이터 영향을 먼저 확인하세요.
기존 사용자 정의 템플릿도 안내를 표시하며 `${error_priority}`로 안내 전체,
`${error_level}`로 수준만 원하는 위치에 넣을 수 있습니다.

[오류 수준과 대응 안내가 포함된 합성 리포트](examples/error-priority-report.md)를 참고하세요.
새 분석에는 분류 정보를 필수로 받고 이전 분류 없는 분석 캐시는 재사용하지 않습니다.
이미 작성된 리포트를 이번 변경으로 일괄 재작성하지는 않습니다.

`NO_SOURCE` 리포트는 소스를 확인하지 못한 결과이며 LLM을 호출하지 않았습니다.
LLM의 confidence는 모델이 표현한 확신 정도로, 장애 원인일 실제 확률은 아닙니다.
발생 횟수는 작성 시점의 보존된 작업 수이고, 기존 리포트가 실시간 갱신되지는 않습니다.
리포트 이후 같은 오류가 새 이벤트로 발생하면 분석 캐시를 재사용할 수 있지만 리포트는
새로 생성합니다. 최초 발생 시각 / 최근 발생 시각은 해당 이벤트 시각이며 오류 그룹의 집계 시각이
아닙니다. 자세한 기준과 사례는 [중복 오류와 재발 처리](DUPLICATE_ERRORS.md)를 참고하세요.

## 7. 주기 실행과 다음 단계

[배포 안내](../deploy/README.md)에 따라 systemd timer를 설치하면 기본 10분마다
실행됩니다. 실행 중 문제가 생기면 [문제 해결](TROUBLESHOOTING.md)을 확인하세요.
전체 설정값은 [설정 안내](CONFIGURATION.md)에 정리되어 있습니다.
