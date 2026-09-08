# 로컬 텍스트 로그로 분석하기

현재 RHEL 8.2용 0.5.0 배포본의 설치·소스 수정 방법은 [폐쇄망 개발 안내](OFFLINE_DEVELOPMENT.md)를 참고하세요.

`0.4.0`부터 ES·Oracle 연결 없이 로컬 파일의 Java 오류를 읽을 수 있습니다.
LLM 분석에는 내부 LLM 서버와 해당 애플리케이션의 로컬 Git 저장소가 필요합니다.
파일 입력은 로그 수집 경로만 대체하며 소스 분석·비밀값 정제·리포트 생성은 같은 처리 과정을 사용합니다.

```mermaid
flowchart LR
    A[완성된 텍스트 로그 파일] --> B[임시 스냅샷]
    B --> C[헤더와 Java stack trace를 한 건으로 묶기]
    C --> D[오류 수준 필터와 이벤트 ID]
    D --> E[SQLite 중복 확인]
    E --> F[Git 소스 확인과 내부 LLM 분석]
    F --> G[Markdown 리포트]
```

## 가장 빠른 시작

폐쇄망 배포 압축을 푼 디렉터리에서 실행합니다.

```bash
mkdir -p data/input
cp config/file-onprem.toml.example config/config.toml
# 실제 로그 대신 형식 확인용 합성 예제를 먼저 복사할 수 있습니다.
cp docs/examples/local-errors.log data/input/application.log
./log-analyzer doctor
```

`config/config.toml`에서 다음 항목을 실제 환경에 맞게 수정합니다.

| 항목 | 설정할 값 |
|---|---|
| `error_source.path` | 분석할 텍스트 로그 파일 한 개의 경로 |
| `error_source.service` | `services`에 정의한 서비스 이름. 예제는 `order_api` |
| `error_source.encoding` | UTF-8은 `utf-8`, Windows 한글 로그는 `cp949` |
| `error_source.timestamp_timezone` | 시간대 없는 헤더의 기준. 한국 시각은 `+09:00` |
| `services.order_api.repository`, `branch` | 실제 애플리케이션의 로컬 Git 저장소·브랜치 |
| `services.order_api.application_packages` | 애플리케이션 Java 패키지 |
| `openai.base_url`, `model`, `tls_ca` | 내부 LLM API·모델·사설 CA |

LLM 인증이 필요하면 기존 방식대로 `config/credentials/LLM_API_KEY`에 키만 저장합니다.
ES·Oracle 인증 정보는 파일 입력에 필요하지 않습니다. 내부 LLM이 무인증 서버일 때만
`auth_required = false`로 설정합니다.

```bash
./log-analyzer check-config
./log-analyzer healthcheck
./log-analyzer run
```

`check-config`는 설정 형식을, `healthcheck`는 파일의 첫 기록 형식·Git·LLM 연결을 확인합니다.
`run`은 전체 파일을 읽어 오류를 처리합니다. 파일 뒤쪽의 형식 오류는 `run`에서 발견될 수 있습니다.
리포트는 `data/reports/`에 저장합니다. 예제 로그의 소스 파일은 별도로 포함되어 있지 않으므로
실제 저장소와 맞지 않으면 `no_source` 리포트가 생성됩니다.

소스 환경에서 실행할 때는 `python -m log_analyzer run --config config/file-onprem.toml.example`을
사용하고, 예제 설정을 복사해 실제 값을 지정합니다. 상대 경로는 실행 디렉터리 기준이며
폐쇄망 실행기는 배포 디렉터리를 기준으로 동작합니다.

## 지원하는 일반 텍스트 형식

```text
2026-09-07 09:01:00.123 ERROR [worker-1] com.example.order.OrderService - Failed to submit order
java.lang.IllegalStateException: bad order
    at com.example.order.OrderService.submit(OrderService.java:42)
Caused by: java.lang.NullPointerException: customer missing
    at com.example.order.OrderService.validate(OrderService.java:30)
2026-09-07 09:02:00.000 INFO [main] Logger - Completed
```

날짜·시각·로그 수준이 있는 헤더에서 한 건을 시작하고, 다음 헤더 직전까지를 같은 기록으로 묶습니다.
`Caused by:`, `Suppressed:`, `... N more`와 여러 줄 stack trace를 기존 Java 파서로 전달합니다.
INFO·WARN 헤더도 기록 경계로 사용하며 기본 분석 대상은 `run.severities`의 ERROR·FATAL입니다.
파일 순서로 처리하므로 시간순으로 정렬되어 있지 않아도 됩니다.

기본 헤더는 `YYYY-MM-DD HH:mm:ss`, 소수 초의 점/쉼표, 날짜와 시각 사이 `T`,
시각 뒤 `Z` 또는 `+09:00` 형태의 오프셋을 지원합니다.
일반 Logback과 Spring Boot의 시각 뒤 로그 수준·PID·스레드·logger가 붙는 형식을 읽습니다.
시각 오프셋이 있으면 그것을 우선하고, 없으면 `timestamp_timezone`을 적용해 UTC로 변환합니다.

헤더에 대괄호가 있는 등 형식이 다르면 `[error_source]`에 정규식을 설정합니다.
예를 들어 `[2026-09-07T09:01:00+09:00] [ERROR] message`는 다음과 같습니다.

```toml
header_pattern = '^\[(?P<timestamp>[^\]]+)\] \[(?P<severity>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\] (?P<message>.*)$'
```

`timestamp`, `severity`, `message`라는 세 그룹이 필요합니다. timestamp는 ISO 형식이어야 합니다.
ERROR뿐 아니라 파일의 **모든 로그 수준 헤더**를 인식하도록 작성하세요. 인식하지 못한 뒤쪽 줄은
이전 기록의 연속 줄로 묶이므로 실제 파일 형식과 맞추는 것이 중요합니다.
헤더 없이 stack trace만 있는 파일, JSONL, 압축 로그, 여러 서비스가 섞인 파일은 이 입력 방식의 대상이 아닙니다.
파일 하나는 설정의 서비스 하나에 대응하며 여러 파일은 설정을 나누어 실행합니다.

## 과거 로그, 중복 오류와 추가 기록

기본 `filter_time_window = false`는 **매번 파일 전체를 읽습니다**. 오래된 로그를 반입해도
일반 배치의 최근 60분 조회 제한 때문에 빠지지 않습니다. 배치 요약의 snapshot 시각과 체크포인트는
실행 기록이며 이 모드의 파일 조회 범위를 제한하지 않습니다.
`true`로 변경하면 ES·Oracle과 같은 lookback·ingestion delay·overlap 시간 범위를 적용합니다.

- 같은 `error_source.name` 아래 같은 바이트 위치·같은 기록 내용은 같은 이벤트 ID입니다.
  상태 DB에 완료 이력이 있으면 재실행 시 분석과 리포트 생성을 건너뜁니다.
- 파일 끝에 완성된 오류 기록을 추가하면 다음 실행에서 새 이벤트로 처리합니다.
  같은 오류라도 다른 위치에 반복되면 별도 이벤트·리포트가 생기고, 조건이 맞으면 LLM 분석 캐시를 재사용합니다.
- 입력을 재정렬하거나 앞부분 삭제, 줄바꿈·인코딩 변경, 로그 로테이션을 하면 바이트 위치나 내용이 달라져
  새 이벤트가 될 수 있습니다. 원본을 편집하지 않고 고정된 반입본을 쓰는 방식을 권장합니다.
- 파일 이름이나 경로만 바뀌고 내용과 source name이 같으면 ID는 유지됩니다. 서로 독립된 로그 묶음에는
  서로 다른 source name을 부여하고, 동일 묶음의 재실행에서는 그 이름과 `data/state.db`를 유지하세요.
- 상태 DB를 삭제하거나 보존기간(기본 30일)으로 완료 이력이 정리되면 이전 기록이 다시 분석될 수 있습니다.
  재시도 작업이 끝난 입력은 별도 보관하고 운영 입력에서 교체하세요.

실행 시작 시 파일을 임시 스냅샷으로 복사하고 페이지별로 읽습니다. 복사 도중 파일이 변경되면
실행을 실패 처리합니다. 복사가 끝난 뒤의 추가·교체 내용은 다음 실행에서 반영됩니다.
프로그램은 끝부분의 Java stack trace가 아직 작성 중인지 알 수 없으므로 **작성 완료된 파일을 반입**하세요.
권한이 제한된 OS 임시 파일을 사용하고 정상 종료 시 닫아 정리합니다. 임시 저장 공간은 입력 파일 크기만큼 필요합니다.

## 오류와 용량 제한

기본 파일 상한은 256 MiB(`max_file_bytes`), 기록 한 건 상한은 1 MiB(`max_record_bytes`)입니다.
LLM에 전달할 문자열은 기존 `analysis.max_log_characters` 상한을 적용합니다.
큰 파일은 로그 헤더 경계를 유지해 별도 파일로 분할하고 각 입력의 source name을 지정하세요.

파일 접근·인코딩·첫 헤더·날짜·용량 오류가 있으면 원문을 출력하지 않고 실행을 실패 처리하며
체크포인트를 전진시키지 않습니다. 앞쪽에서 이미 처리한 건은 남으므로 파일을 올바르게 준비해
재실행하면 완료 이력을 이용합니다. 일반 텍스트에는 고유 ID가 없기 때문에 오류 수정으로 기존 기록의
바이트 위치까지 바뀐 경우에는 동일 이벤트로 판단되지 않을 수 있습니다.

설정 예제: [file-onprem.toml.example](../config/file-onprem.toml.example)
합성 로그: [local-errors.log](examples/local-errors.log)
기존 입력: [Oracle](ORACLE.md), [전체 설정](CONFIGURATION.md)
