# 실행별 설정으로 여러 로그와 저장소 분석하기

하나의 설치본에서 `--config`에 지정하는 TOML 파일만 바꾸어 실행합니다.
각 설정은 독립된 분석 작업을 정의하며 프로그램 소스를 복사하거나 수정할 필요가 없습니다.
현재 분석 언어는 Java이며 `[analysis] language = "java"`로 명시합니다.

## 설정 두 개로 시작하기

Linux/WSL의 프로젝트 루트 또는 폐쇄망 배포 루트에서 예제를 복사합니다.

```bash
cp config/orders.toml.example config/orders.toml
cp config/payments.toml.example config/payments.toml
```

각 파일의 로그 경로, 서비스 이름, Git 경로·브랜치·Java 패키지,
내부 LLM 주소·모델을 실제 환경에 맞게 수정합니다. 인증키는 TOML에 직접 넣지 않고
`LLM_API_KEY` 환경변수에 제공합니다. 폐쇄망 실행기는
`config/credentials/LLM_API_KEY` 파일도 읽습니다. 사설 CA가 필요하면 `openai.tls_ca`를 지정합니다.

소스 설치본에서는 같은 Python 환경으로 다음과 같이 실행합니다.

```bash
python -m log_analyzer healthcheck --config config/orders.toml
python -m log_analyzer run --config config/orders.toml
python -m log_analyzer run --config config/payments.toml
```

폐쇄망 배포본에서는 포함된 실행기에 같은 옵션을 전달합니다.

```bash
./log-analyzer check-config --config config/orders.toml
./log-analyzer healthcheck --config config/orders.toml
./log-analyzer run --config config/orders.toml
./log-analyzer run --config config/payments.toml
```

`check-config`는 폐쇄망 실행기의 설정 형식 검사 명령입니다. 소스 CLI의
`healthcheck`는 입력 파일·Git·LLM 연결까지 확인합니다. 전체 배치 실행의 잠금은
POSIX 기반이므로 Linux/WSL에서 실행합니다.

## 실행별로 바꾸는 항목

| 목적 | TOML 항목 | 주문 예제 | 결제 예제 |
|---|---|---|---|
| 입력 로그 | `error_source.path` | `input/orders/application.log` | `input/payments/application.log` |
| 입력 식별자 | `error_source.name` | `orders-file-logs` | `payments-file-logs` |
| 서비스 연결 | `error_source.service`와 `[services.<이름>]` | `orders` | `payments` |
| 분석 언어 | `analysis.language` | `java` | `java` |
| 로컬 Git 저장소 | `services.<이름>.repository` | `repos/orders.git` | `repos/payments.git` |
| 분석 브랜치 | `services.<이름>.branch` | `main` | `main` |
| Java 소스 루트 | `services.<이름>.source_roots` | `src/main/java` | `src/main/java` |
| 애플리케이션 패키지 | `services.<이름>.application_packages` | `com.example.order` | `com.example.payment` |
| 결과 저장 위치 | `report.directory` | `reports/orders` | `reports/payments` |
| 처리 이력·캐시 | `state.path` | `state/orders/state.db` | `state/payments/state.db` |
| 중복 실행 잠금 | `run.lock_file` | `state/orders/run.lock` | `state/payments/run.lock` |

파일 입력은 설정 하나당 로그 파일 하나와 서비스 하나를 지정합니다. 여러 로그 파일은
설정을 나누어 실행합니다. ES·Oracle 입력도 같은 `--config` 방식을 사용하며
각 입력에 맞게 `[error_source]`를 교체합니다. 한 설정의 `[services]`에는 여러 저장소를
등록할 수 있고, 각 이벤트의 서비스 이름으로 저장소를 선택합니다.

소스 CLI에서 TOML 내부 상대 경로는 **명령을 실행한 디렉터리 기준**입니다.
폐쇄망 실행기는 배포 루트로 이동한 뒤 실행하므로 **배포 루트 기준**입니다.
설정 파일이 위치한 디렉터리를 기준으로 해석하지 않습니다. 외부 설정 파일을 주입하거나
여러 디렉터리에서 호출할 때는 로그·Git·상태·잠금·결과·CA 경로를 절대 경로로 지정하면 됩니다.

## 결과 파일과 재실행

`report.directory` 아래 오류 이벤트별 Markdown 파일을 자동으로 생성합니다.
개별 파일명은 입력 식별자와 이벤트 ID로 정해지며, 현재 설정으로 고정 파일명 하나를
지정하거나 전체 오류를 하나의 보고서로 합치지는 않습니다.

같은 설정과 상태 DB로 재실행하면 완료한 이벤트를 건너뛰고 새 오류와 재시도 대상을 처리합니다.
서로 다른 분석 작업은 **상태 DB·잠금·결과 디렉터리를 모두 분리**하세요.
입력 이름도 구분하면 로그 묶음의 출처를 추적하기 쉽습니다. 잠금을 분리하면 다른 설정을
동시에 실행할 수 있으며, 같은 설정의 중복 실행은 기존 잠금으로 차단됩니다.

같은 로그를 다른 저장소·브랜치로 비교하거나 LLM 모델을 바꾸어 처음부터 다시 분석할 때도
새 설정에 별도의 상태 DB와 결과 디렉터리, 잠금을 지정합니다. 기존 상태 DB를 그대로 쓰면
이미 완료한 이벤트는 건너뛰므로 결과 경로만 바꾸어도 새 보고서가 생성되는 것은 아닙니다.
기존 분석 기록을 유지하려면 해당 DB를 삭제하지 않고 새 경로를 사용합니다.

Git 저장소는 실행 전에 준비하고 필요한 커밋·브랜치를 로컬에 반입해야 합니다.
프로그램은 자동 clone/fetch/checkout을 수행하지 않습니다.

예제: [주문 설정](../config/orders.toml.example), [결제 설정](../config/payments.toml.example)

관련 문서: [파일 입력 형식](LOCAL_FILES.md), [전체 설정](CONFIGURATION.md),
[중복 오류 처리](DUPLICATE_ERRORS.md)
