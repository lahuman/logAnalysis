# logAnalysis

**Java 오류 로그와 소스 코드를 함께 읽고, 원인 후보·수정 방법·검증 절차를 리포트로 정리하는 도구입니다.**

반복되는 오류를 사람이 하나씩 찾아보는 시간을 줄이는 것이 목표입니다.
Elasticsearch에서 오류를 가져오고, Java stack trace로 Git 소스 위치를 찾은 뒤,
LLM 분석을 검증해 Markdown으로 저장합니다. 최종 판단과 코드 수정은 사람이 합니다.

```text
Elasticsearch 오류 → Java 예외·소스 위치 파싱 → Git 소스 확인
                  → LLM 분석 → 파일·라인 근거 검증 → Markdown 리포트
```

로그에 Git commit이 없어도 사용할 수 있습니다. 설정한 브랜치의 소스를 읽고
오류 라인의 blame과 해당 파일의 최근 변경 이력을 참고합니다. 이 경우 실제 배포
버전은 알 수 없으므로 변경 이력만으로 장애 원인을 확정하지 않습니다.

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
RHEL 9 x86_64용 배포본에 **Python 3.11.8, 의존성, 로컬 조회용 Git**을 포함합니다.
압축을 풀고 `./log-analyzer doctor`로 점검한 뒤 내부 ES·LLM 주소를 설정하면 됩니다.
실행 시 인터넷 설치·모델 다운로드 없이 내부 Chat Completions 서버를 사용합니다.
LLM 서버와 모델 가중치는 별도로 준비합니다.

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

1. Elasticsearch 로그에 시간·서비스명·오류 메시지·stack trace가 있는지 확인
2. 분석할 서비스의 Git 저장소와 Java package 준비
3. 전체 설정 파일에 Elasticsearch, LLM, 소스와 결과 경로 지정
4. `healthcheck` 후 `run`으로 한 번 분석
5. 필요하면 systemd timer로 주기 실행

| 사용할 기능 | 실행 환경 | 준비물 |
|---|---|---|
| 샘플 오류의 NIM 분석 | Windows / Linux / WSL | Python 3.11.x, API 키 |
| 내 Elasticsearch 로그 배치 분석 | Linux / WSL | 위 항목 + HTTPS Elasticsearch + 로컬 Git 저장소 |
| 정기 분석 | systemd가 있는 Linux | 배치 설정 + 전용 서비스 계정 |

현재 전용 파서는 Java이며 LLM은 **내부 onprem 서버, NVIDIA NIM 또는 OpenAI**를 선택합니다.
일반 로그 파일을 직접 업로드하는 기능과 웹 UI는 없습니다. 서비스 로그는
Elasticsearch에 수집되어 있어야 합니다. Windows에서는 전체 배치 대신 샘플 분석과
단위 테스트를 실행할 수 있습니다.

## 문서 찾기

| 필요한 내용 | 문서 |
|---|---|
| 중요망 브랜치의 전체 흐름·구현·후속 과제 | [브랜치 작업 정리](docs/IMPORTANT_NETWORK.md) |
| 중요망 압축 해제·내부 LLM 설정·운영 | [중요망 배포 안내](docs/OFFLINE_DEPLOYMENT.md) |
| 내 로그 연결, 실행, 리포트 읽기 | [사용 가이드](docs/USAGE.md) |
| 중복 판별, 재발, 분석 캐시와 리포트 생성 | [중복 오류 처리](docs/DUPLICATE_ERRORS.md) |
| 설정값, 인증, Git 소스 선택 | [설정 안내](docs/CONFIGURATION.md) |
| 인증 실패, NO_SOURCE, 빈 결과 해결 | [문제 해결](docs/TROUBLESHOOTING.md) |
| NVIDIA NIM 모델·출력 모드 | [NIM 연동](docs/NVIDIA_NIM.md) |
| RHEL 설치와 주기 실행 | [배포 안내](deploy/README.md) |
| 개발·Docker·Elasticsearch 테스트 | [테스트 안내](docs/WSL_DOCKER_TESTING.md) |
| 구현·실연동 검증 범위 | [검증 현황](docs/VALIDATION_STATUS.md) |
| 내부 구조와 설계 | [시스템 설계](SYSTEM_DESIGN.md) |

## 현재 상태

버전 `0.2.0`. 2026-09-05 기준 Java 파싱, Git 소스·변경 이력 조회, LLM 응답 검증,
중복 분석 캐시, 재시도·중단 복구, Markdown 출력과 systemd 설정을 구현했습니다.

- Windows / Python 3.11.8: **123개 중 118 통과, 5개 조건부 제외**
- 중요망 런타임 / Rocky Linux 9 / Python 3.11.8: **123개 중 119 통과, 4개 조건부 제외**
- 압축파일: 일반 사용자·인터넷 차단·공백 경로에서 실행, 내부 HTTP/HTTPS·사설 CA·Git·리포트 검증 통과
- Rocky Linux + 실제 Elasticsearch 8.19.21: NIM 추가 전 **99개 중 98 통과, 1개 제외**
- NVIDIA NIM: 실제 샘플 분석·JSON 검증·리포트 생성 성공
- 실제 온프레미스 모델, ES → Git → NIM 전체 연결, OpenAI 실제 생성, RHEL 9 systemd 최종 검증은 후속 검증 대상

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
