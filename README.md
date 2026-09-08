# logAnalysis

**0.5.0: RHEL 8.2 / glibc 2.28 호환 및 소스 직접 수정 실행** — [폐쇄망 소스 수정 안내](docs/OFFLINE_DEVELOPMENT.md).

**Java 오류 로그와 소스 코드를 함께 읽고, 원인 후보·수정 방법·검증 절차를 리포트로 정리하는 도구입니다.**

반복되는 오류를 사람이 하나씩 찾아보는 시간을 줄이는 것이 목표입니다.
로컬 텍스트 파일, Elasticsearch 또는 Oracle SQL에서 오류를 가져오고, Java stack trace로 Git 소스 위치를 찾은 뒤,
LLM 분석을 검증해 Markdown으로 저장합니다. 최종 판단과 코드 수정은 사람이 합니다.

```text
파일 / ES / Oracle 오류 → Java 예외·소스 위치 파싱 → Git 소스 확인
                  → LLM 분석 → 파일·라인 근거 검증 → Markdown 리포트
```

로그에 Git commit이 없어도 사용할 수 있습니다. 설정한 브랜치의 소스를 읽고
오류 라인의 blame과 해당 파일의 최근 변경 이력을 참고합니다. 이 경우 실제 배포
버전은 알 수 없으므로 변경 이력만으로 장애 원인을 확정하지 않습니다.

**[그림으로 보는 전체 구성·처리 흐름·폐쇄망 설치](docs/ARCHITECTURE.md)**에서
Archify로 작성한 도식 3종과 브라우저용 HTML을 확인할 수 있습니다.

## 어떤 결과를 얻나요?

예를 들어 아래 오류를 분석하면:

```text
java.lang.NullPointerException: Cannot invoke "String.trim()" because "customer" is null
    at com.example.smoke.OrderService.customerName(OrderService.java:4)
```

리포트에 오류 요약, `OrderService.java:4`의 근거, null 검사 등의 수정안,
null·정상 입력 회귀 테스트와 추가 확인할 사항이 담깁니다.

**[실제 NVIDIA NIM으로 생성한 샘플 리포트 보기](docs/examples/nim-report.md)** — 합성 Java 오류를 사용했으며 실제 운영 로그는 포함하지 않습니다.

## 중요망·폐쇄망에서 사용하기

현재 중요망 작업은 **`codex/important-network` 브랜치**에 있습니다.
[브랜치 작업 정리](docs/IMPORTANT_NETWORK.md)에서 전체 절차, 구현 범위, 검증 결과와
후속 과제를 확인할 수 있습니다. 소스를 받을 때는 다음처럼 브랜치를 지정하세요.

```bash
git clone --branch codex/important-network https://github.com/lahuman/logAnalysis.git
```

**[압축 해제형 설치 안내](docs/OFFLINE_DEPLOYMENT.md)**를 따르세요.
RHEL 8.2 x86_64용 배포본에 **Python 3.11.8, 의존성, 로컬 조회용 Git**을 포함합니다.
압축을 풀고 `./log-analyzer doctor`로 점검한 뒤 로그 파일과 내부 LLM·Git 경로를 설정하면 됩니다.
실행 시 인터넷 설치·모델 다운로드 없이 내부 Chat Completions 서버를 사용합니다.
LLM 서버와 모델 가중치는 별도로 준비합니다.

**[로컬 텍스트 로그 입력 안내](docs/LOCAL_FILES.md)**: `0.5.0` 배포본의 기본 입력입니다.
시각·로그 수준 헤더와 Java stack trace를 한 건으로 묶고 과거 파일도 전체 조회합니다.
같은 기록의 재실행은 상태 DB로 건너뜁니다. ES·Oracle 입력도 선택할 수 있습니다.

**[Oracle SQL 연결 안내](docs/ORACLE.md)**: `0.3.0`부터 Oracle 테이블·뷰 조회를 선택할 수 있습니다.
예시 설정, 컬럼 매핑, 인증 파일, 시간대, 중복·실패 처리와 실제 DB 검증 절차를 제공합니다.
Oracle Client 설치 없이 사용하며, 실제 DB 연결은 접속 정보가 준비된 뒤 확인해야 합니다.

## 인터넷 연결 환경에서 가장 빠르게 시작하기

먼저 Elasticsearch 없이 샘플 오류 한 건을 실제 LLM으로 분석해 보세요.
필요한 것은 **Git, Python 3.11.x, NVIDIA API 키**입니다. 검증 버전은 Python 3.11.8입니다.
NVIDIA 호스팅 API를 사용하므로 내 컴퓨터에 GPU를 설치할 필요는 없습니다.

### 1. 내려받고 설치하기

Linux / WSL:

```bash
git clone https://github.com/lahuman/logAnalysis.git
cd logAnalysis
python3.11 -m venv .venv
.venv/bin/python -m pip install .
```

Windows PowerShell:

```powershell
git clone https://github.com/lahuman/logAnalysis.git
cd logAnalysis
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install .
```

### 2. API 키 준비하기

[NVIDIA API 키 발급 안내](https://docs.api.nvidia.com/nim/docs/api-quickstart)에 따라
NVIDIA Build에서 키를 발급받습니다. `config/credentials` 폴더를 만들고
`NVIDIA_API_KEY`라는 파일에 **키 값만** 저장하세요. 확장자 `.txt`나 따옴표를 붙이지 않습니다.

Linux / WSL에서는 다음과 같이 입력할 수 있습니다. 입력한 키는 화면에 표시되지 않습니다.

```bash
mkdir -p config/credentials
chmod 700 config/credentials
read -r -s -p 'NVIDIA API key: ' NVIDIA_API_KEY
printf '%s' "$NVIDIA_API_KEY" > config/credentials/NVIDIA_API_KEY
chmod 600 config/credentials/NVIDIA_API_KEY
unset NVIDIA_API_KEY
```

Windows에서는 탐색기나 편집기로 위 파일을 만들어 저장하면 됩니다. 키 파일과
생성 리포트는 Git 제외 대상입니다. 이미 `NVIDIA_API_KEY` 환경 변수를 사용 중이라면
파일을 만들지 않아도 되며, 환경 변수의 값이 파일보다 우선합니다.

### 3. 첫 리포트 생성하기

Linux / WSL:

```bash
.venv/bin/python -m log_analyzer.nim_smoke \
  --config config/nvidia-nim.toml.example \
  --credentials-directory config/credentials
```

Windows PowerShell:

```powershell
.venv/Scripts/python.exe -m log_analyzer.nim_smoke --config config/nvidia-nim.toml.example --credentials-directory config/credentials
```

성공하면 `nim_smoke_succeeded`와 리포트 파일 경로가 출력됩니다.
`reports/nim-smoke/`의 Markdown 파일을 열어 결과를 확인하세요.
이 명령은 내 로그나 Git 저장소를 읽지 않고 내장된 샘플만 전송합니다.
API 호출은 계정의 사용량·요금 정책을 따릅니다.

## 내 오류 로그에 연결하기

**[사용 가이드](docs/USAGE.md)**에서 다음 순서로 진행합니다.

1. 로컬 텍스트 파일 또는 ES·Oracle 입력을 선택하고 시각·오류 메시지·stack trace 확인
2. 분석할 서비스의 Git 저장소와 Java package 준비
3. 전체 설정 파일에 선택한 입력, LLM, 소스와 결과 경로 지정
4. `healthcheck` 후 `run`으로 한 번 분석
5. 필요하면 systemd timer로 주기 실행

| 사용할 기능 | 실행 환경 | 준비물 |
|---|---|---|
| 샘플 오류의 NIM 분석 | Windows / Linux / WSL | Python 3.11.x, API 키 |
| 내 로그 배치 분석 | Linux / WSL | 텍스트 파일 또는 ES·Oracle, 로컬 Git 저장소, 선택한 LLM 연결 |
| 정기 분석 | systemd가 있는 Linux | 배치 설정 + 전용 서비스 계정 |

현재 전용 파서는 Java이며 LLM은 **내부 onprem 서버, NVIDIA NIM 또는 OpenAI**를 선택합니다.
로컬 텍스트 로그는 파일 경로로 지정하며 웹 UI는 없습니다.
Windows에서는 전체 배치 대신 샘플 분석과
단위 테스트를 실행할 수 있습니다.

## 문서 찾기

| 필요한 내용 | 문서 |
|---|---|
| 중요망 브랜치의 전체 흐름·구현·후속 과제 | [브랜치 작업 정리](docs/IMPORTANT_NETWORK.md) |
| 중요망 압축 해제·내부 LLM 설정·운영 | [중요망 배포 안내](docs/OFFLINE_DEPLOYMENT.md) |
| 내 로그 연결, 실행, 리포트 읽기 | [사용 가이드](docs/USAGE.md) |
| 중복 판별, 재발, 분석 캐시와 리포트 생성 | [중복 오류 처리](docs/DUPLICATE_ERRORS.md) |
| 설정값, 인증, Git 소스 선택 | [설정 안내](docs/CONFIGURATION.md) |
| 실행별 설정 주입, 여러 로그·Git 저장소와 결과 분리 | [실행별 설정 안내](docs/RUN_PROFILES.md) |
| 오류 발생 파일·함수·줄 번호 확인, 인증 실패·NO_SOURCE 해결 | [문제 해결](docs/TROUBLESHOOTING.md) |
| NVIDIA NIM 모델·출력 모드 | [NIM 연동](docs/NVIDIA_NIM.md) |
| RHEL 설치와 주기 실행 | [배포 안내](deploy/README.md) |
| 개발·Docker·Elasticsearch 테스트 | [테스트 안내](docs/WSL_DOCKER_TESTING.md) |
| 구현·실연동 검증 범위 | [검증 현황](docs/VALIDATION_STATUS.md) |
| 내부 구조와 설계 | [시스템 설계](SYSTEM_DESIGN.md) |

## 현재 상태

버전 `0.5.0`. 2026-09-08 기준 파일·ES·Oracle 입력, Java 파싱, Git 소스·변경 이력 조회,
LLM 응답 검증, 중복 분석 캐시, 재시도·중단 복구, Markdown 출력과 systemd 설정을 구현했습니다.
실행 오류에는 파일·함수·줄 번호와 원인 예외 체인을 남기며 개별 분석 실패도 이벤트 ID로 추적합니다.

- Windows / Python 3.11.8: **169개 중 163 통과, 6개 조건부 제외**. compileall·pip check 통과
- RHEL 8.2 / glibc 2.28 대상 빌드 검사, 소스 직접 수정 실행, 동봉 테스트 명령을 추가
- 과거 실제 ES·NIM 연결과 압축파일 검증 결과는 [검증 현황](docs/VALIDATION_STATUS.md)에 시점별 기록
- 이번 오류 로그 변경을 포함한 배포 압축파일 재빌드·실서버 연결 검증은 별도 수행 필요

오류와 소스는 제한된 범위로 정제하여 선택한 LLM API에 전송합니다. 마스킹만으로
모든 비밀정보를 식별할 수는 없으므로 전송 가능한 데이터로 시작하세요.
리포트는 검토를 돕는 분석 결과이며 자동으로 코드를 수정하거나 배포하지 않습니다.

## 개발 및 피드백

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m pip check
```

재현 가능한 문제나 개선 제안은 [GitHub Issues](https://github.com/lahuman/logAnalysis/issues)에
남겨주세요. OS·Python 버전, 종료 코드, 기대 결과와 비식별 오류 예시가 있으면 도움이 됩니다.
API 키, 운영 원문 로그, 비공개 소스 코드는 첨부하지 마세요.
