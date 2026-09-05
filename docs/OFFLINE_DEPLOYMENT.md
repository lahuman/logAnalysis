# 중요망에서 압축을 풀고 사용하기

이 배포본은 **RHEL 9 x86_64 / Python 3.11.8**을 기준으로 합니다.
Python 3.11.8, Python 의존성, 로컬 소스 조회용 Git을 포함하므로 대상 서버에서
`pip install`, `dnf install`, 가상환경 생성, 컨테이너 실행이 필요하지 않습니다.
일반 사용자 권한으로 압축을 풀고 실행합니다.

LLM 제품과 모델은 내부 서버가 선정된 후 설정합니다. 분석기는 내부 Elasticsearch와
OpenAI 호환 **Chat Completions API**에 연결합니다. LLM GPU 서버·모델 가중치와
Elasticsearch 자체는 별도로 준비하는 서버 구성요소입니다.

## 1. 압축 해제와 실행 점검

반입할 파일 두 개:

- `log-analyzer-0.2.0-rhel9-x86_64-python3.11.8.tar.gz`
- `log-analyzer-0.2.0-rhel9-x86_64-python3.11.8.tar.gz.sha256`

쓰기·실행이 가능한 디렉터리에서 실행합니다. 아래 경로는 예시이며 다른 위치에 풀어도 됩니다.

```bash
sha256sum -c log-analyzer-0.2.0-rhel9-x86_64-python3.11.8.tar.gz.sha256
tar -xzf log-analyzer-0.2.0-rhel9-x86_64-python3.11.8.tar.gz
cd log-analyzer-0.2.0
./log-analyzer doctor
```

`offline_doctor_succeeded`가 나오면 Python 버전, 필수 라이브러리, Git, SQLite,
파일 쓰기·잠금, 배포 파일 체크섬 검사가 통과한 것입니다. **외부/내부 네트워크 모두
사용하지 않는 검사**이므로 LLM이나 ES 설정 전에도 실행할 수 있습니다.
`manifest.json`에는 파일 체크섬, Python 배포 출처, 의존성 버전·다운로드 URL이 들어 있습니다.

서버에 Python 3.11.8이 이미 설치되어 있어도 기본값은 동봉된 런타임입니다.
서버 Python을 사용하려면 정확히 3.11.8인 실행 파일을 지정합니다. 의존성은 계속
배포본의 `app/`에서 읽으며 시스템 패키지를 설치하거나 바꾸지 않습니다.

```bash
LOG_ANALYZER_PYTHON=/usr/local/bin/python3.11 ./log-analyzer doctor
```

## 2. 내부 주소와 소스 설정

`config/config.toml`을 편집합니다. 처음 반입한 파일에는 설명을 위한 예시 값이 들어 있습니다.
실제 연결에는 아래 정보가 필요합니다.

| 설정 | 입력할 값 |
|---|---|
| `error_source.url` | 내부 Elasticsearch HTTPS 주소 |
| `error_source.index` | 오류 로그 인덱스 패턴 |
| `error_source.tls_ca` | ES 인증서를 발급한 내부 CA PEM 파일 |
| `services.order_api` | 로그의 `service.name`에 맞는 테이블 이름 |
| `repository` | 반입한 Git 저장소 경로; 기본 `repos/order-api.git` |
| `branch` | 저장소에 존재하는 브랜치 또는 ref |
| `application_packages` | 애플리케이션 Java 패키지 접두사 |
| `openai.base_url` | 내부 LLM API 루트; `/v1`까지 입력 |
| `openai.model` | 내부 서버 `/v1/models`에 등록된 정확한 모델 ID |
| `openai.tls_ca` | LLM 서버의 내부 CA PEM 파일 |

호환성을 위해 LLM 설정 테이블 이름은 `[openai]`를 사용하지만,
`provider = "onprem"`은 내부 서버의 `/v1/chat/completions`를 호출합니다.
배포 실행기는 다른 provider 설정을 거부합니다. 경로가 상대 경로이면
**현재 터미널 위치와 무관하게 압축을 푼 디렉터리 기준**으로 해석합니다.

HTTPS와 인증을 사용하는 기본 설정:

```toml
[openai]
provider = "onprem"
base_url = "https://llm.internal:8000/v1"
model = "your-internal-served-model"
tls_ca = "config/certs/internal-ca.pem"
auth_required = true
api_key_secret = "LLM_API_KEY"
structured_output = "json_schema"
timeout_seconds = 120
max_output_tokens = 4096
```

내부 서버가 HTTP·무인증으로 제공되는 경우에는 아래처럼 **명시적으로** 설정합니다.
이 경우 LLM의 `tls_ca` 항목은 제거합니다. Elasticsearch는 기존대로 HTTPS가 필요합니다.

```toml
[openai]
provider = "onprem"
base_url = "http://llm.internal:8000/v1"
model = "your-internal-served-model"
allow_http = true
auth_required = false
structured_output = "json_schema"
timeout_seconds = 120
max_output_tokens = 4096
```

사설 CA는 `config/certs/`에 넣습니다. 인증서 검증을 끄는 옵션은 없습니다.
공인 CA 인증서를 사용하면 `tls_ca`를 생략할 수 있습니다.
`auth_required=false`여도 `LLM_API_KEY`가 환경 변수나 파일에 있으면 Bearer 토큰을 보냅니다.

## 3. 인증 정보와 Git 소스 준비

인증이 필요하면 키만 담은 파일을 만듭니다. 아래 명령은 입력값을 화면에 표시하지 않습니다.

```bash
chmod 700 config/credentials
read -rsp 'Internal LLM API key: ' LLM_KEY; printf '\n'
(umask 077; printf '%s' "$LLM_KEY" > config/credentials/LLM_API_KEY)
unset LLM_KEY
```

ES는 같은 디렉터리에 `ES_API_KEY`를 넣거나 `ES_USERNAME`, `ES_PASSWORD` 파일을
함께 넣습니다. 환경 변수에 같은 이름이 있으면 파일보다 우선합니다.
키·CA·운영 설정·운영 로그·실제 Git 저장소는 기본 배포본에 포함하지 않습니다.

소스는 **커밋 이력을 포함한 완전한 Git 저장소**를 별도로 반입합니다. 담당자가 인터넷망이나
내부 개발망에서 승인된 저장소를 mirror clone한 뒤 압축해 전달할 수 있습니다.
저장소가 이미 중요망에 있다면 `repository`를 그 절대 경로로 설정해도 됩니다.
단순 소스 ZIP에는 Git 이력이 없어 분석기의 소스 조회에 사용할 수 없습니다.
partial/shallow clone은 필요한 커밋을 누락할 수 있으므로 사용하지 않습니다.
동봉 Git은 분석에 필요한 로컬 명령용이며 원격 저장소 동기화는 운영자가 별도로 수행합니다.

## 4. 첫 분석 실행

```bash
./log-analyzer check-config
./log-analyzer smoke
./log-analyzer healthcheck
./log-analyzer run
```

| 명령 | 확인하는 내용 | 통신 |
|---|---|---|
| `doctor` | Python·Git·라이브러리·파일 무결성·SQLite·잠금 | 없음 |
| `check-config` | 설정 형식, onprem 선택, 모델 예시 값 교체 | 없음 |
| `smoke` | 합성 Java 오류 한 건을 분석하고 `data/smoke-reports/`에 저장 | 설정된 내부 LLM만 |
| `healthcheck` | ES·Git·SQLite와 LLM 모델 목록 확인; 추론은 하지 않음 | 내부 ES·LLM |
| `run` | 실제 로그 조회부터 분석·리포트 저장까지 한 번 실행 | 내부 ES·LLM |

`smoke` 성공은 구조화 응답과 근거·수정안·검증 절차를 실제로 받았다는 의미입니다.
`healthcheck`에서 모델 목록이 조회돼도 추론 권한까지 보장되지는 않습니다.
샘플 명령의 JSON 이벤트 이름에는 기존 호환을 위한 `nim_smoke_*`가 표시됩니다.
실제 연결 대상은 설정한 내부 LLM입니다.

실제 리포트는 `data/reports/`, 체크포인트·중복 제거·재시도 상태는 `data/state.db`에 저장합니다.
같은 설정으로 `run`을 반복하면 저장된 체크포인트에서 이어갑니다.
리포트의 원인 후보와 제안을 사람이 검토한 뒤 수정·배포를 결정합니다.
로그 필드와 결과 읽는 법은 저장소의 `docs/USAGE.md`를 참고하세요.

## 5. 주기 실행과 교체

수동 실행이 확인되면 기존 작업 스케줄러에 `log-analyzer run`을 등록할 수 있습니다.
cron을 사용한다면 실행 계정의 crontab에 다음과 같이 등록합니다. 경로에 공백이 없는 예입니다.

```cron
*/10 * * * * /srv/log-analyzer-0.2.0/log-analyzer run >> /srv/log-analyzer-0.2.0/data/batch.log 2>&1
```

실행이 겹치면 파일 잠금으로 다음 실행을 건너뜁니다. `batch.log`는 조직의 logrotate 정책으로 관리합니다.
systemd를 사용할 때도 `ExecStart`는 이 배포본의 `log-analyzer run`을 가리키게 합니다.
인터넷 설치용 service 파일을 그대로 복사하면 경로가 맞지 않을 수 있습니다.

교체할 때는 스케줄러를 중지하고 진행 중인 배치가 끝난 것을 확인합니다. 새 버전을
**새 디렉터리**에 풀고 `config/`, `repos/`, `data/`를 복사하거나 기존 절대 경로를 사용합니다.
SQLite는 배치가 멈춘 상태에서 관련 DB/WAL/SHM 파일을 함께 보관합니다.
새 버전의 `doctor`·`healthcheck` 후 스케줄러 실행 경로를 바꿉니다. 기존 디렉터리는 검증이 끝날 때까지 보관합니다.

## 통신 범위와 LLM 호환성

분석 시 설정된 내부 ES와 LLM만 사용합니다. 실행 중 패키지 설치·모델 다운로드·Git fetch·
클라우드 LLM 대체 호출은 하지 않습니다. 런처는 HTTP 프록시 환경 변수를 제거하며,
onprem LLM 클라이언트도 환경 변수 프록시를 사용하지 않습니다.
내부 DNS와 CA를 설정하고, 서버의 송신 방화벽에서는 승인된 내부 ES·LLM·DNS만 허용하세요.
애플리케이션 설정만으로 임의의 DNS 이름이 실제 내부 주소인지 보장하지는 않습니다.

서버는 `/v1/models`와 비스트리밍 `/v1/chat/completions`, JSON 구조화 출력을 제공해야 합니다.
기본은 `json_schema`입니다. 서버의 제품·버전·모델에 맞게 `json_object` 또는
NIM의 `guided_json`을 명시적으로 선택할 수 있습니다. 지원하지 않는 형식으로 실패하면
설정을 수정해야 하며 자동으로 형식을 바꾸거나 다른 공급자를 호출하지 않습니다.
`enable_thinking`은 서버가 `chat_template_kwargs`를 지원할 때만 설정합니다.

서버 설치 담당자는 NVIDIA의 [NIM API 문서](https://docs.nvidia.com/nim/large-language-models/1.5.0/api-reference.html)와
[폐쇄망 배포 안내](https://docs.nvidia.com/nim/large-language-models/1.8.0/index.html)를 참고할 수 있습니다.
실제 제품과 모델이 정해지면 해당 버전의 지원 형식과 GPU 조건을 확인하고 `smoke`로 검증합니다.

## 자주 발생하는 문제

| 현상 | 확인할 사항 |
|---|---|
| `Permission denied` | 압축 해제 디렉터리의 실행 권한, `noexec` 마운트, SELinux 정책 |
| Python 버전 오류 | `LOG_ANALYZER_PYTHON`을 해제하면 동봉 3.11.8 사용 |
| `offline_preflight_failed` | 표시된 원인, 설정 TOML 형식, 파일·디렉터리 권한 확인 |
| `bundle file mismatch` | 동일 배포본을 새 디렉터리에 다시 풀고 checksum 확인; 임의 라이브러리 수정 여부 확인 |
| `runtime_configuration_invalid` | `LLM_API_KEY` 등 인증 파일 존재와 읽기 권한 확인 |
| TLS 연결 실패 | LLM/ES 각각의 CA, 인증서의 DNS 이름, 서버 시각 확인 |
| HTTP 401/403 | 내부 서버 인증 방식과 키·추론 권한 확인 |
| HTTP 400/422 | 모델 ID, JSON 출력 형식, thinking 옵션·토큰 상한 확인 |
| 모델 목록에 없음 | `/v1/models`의 served model ID를 정확히 입력 |
| `no_source` | 서비스 이름·패키지·branch·반입 Git의 커밋 이력 확인 |

## 배포 담당자: 인터넷망에서 압축파일 만들기

일반 사용자는 위의 완성 압축파일을 받아 사용하면 됩니다. 아래는 새 버전을 만드는 절차입니다.
인터넷이 연결된 Linux x86_64 호스트에서 Podman 또는 Docker를 사용합니다.
**빌드만** 네트워크가 필요하며 이 도구는 대상 서버에 설치하지 않습니다.

```bash
podman build -t log-analyzer-offline-builder -f deploy/offline/Containerfile .
podman run --rm -v "$PWD:/source:Z" log-analyzer-offline-builder
```

결과는 `dist/offline/`에 생성됩니다. 출력 파일이 이미 있으면 덮어쓰지 않으므로
다음 빌드는 `--output /source/dist/offline-next`로 별도 디렉터리를 지정합니다.
빌더는 소스·템플릿·문서·예시 설정만 선택해 담으며 로컬 키나 운영 데이터는 복사하지 않습니다.

Python은 [python-build-standalone](https://gregoryszorc.com/docs/python-build-standalone/main/running.html)의
3.11.8/20240224 배포판을 체크섬 검증 후 포함합니다. Linux wheel 24개는
`deploy/offline/wheels.lock.json`에 버전·URL·SHA-256으로 고정되어 있습니다.
Git은 로컬 명령에 필요한 부분을 RHEL 9 호환 컨테이너에서 빌드하며 zlib를 정적으로 연결합니다.
런타임 glibc는 RHEL 9의 기본 glibc 2.34 이상을 사용합니다.
Git·zlib 원본 소스와 라이선스, 빌드 명령을 압축파일의 `third-party/`에 포함합니다.
Python·wheel의 동봉 라이선스도 보존합니다.

일반 사용자 권한과 외부 통신이 차단된 공간에서 압축파일 자체를 재검증하려면,
테스트 Linux 호스트에서 다음을 실행합니다. 테스트 호스트에는 `unshare`, `ip`,
`openssl`, Python 3.11이 필요합니다. 분석 대상 서버의 설치 요구사항은 아닙니다.

```bash
sudo unshare --net sh -c 'ip link set lo up; runuser -u nobody -- python3.11 deploy/offline/verify.py dist/offline/log-analyzer-0.2.0-rhel9-x86_64-python3.11.8.tar.gz'
```

`nobody` 계정이 저장소와 압축파일을 읽을 수 있어야 합니다. 검증기는 쓰기 가능한 임시
디렉터리에 풀고, 합성 HTTP/HTTPS 서버로 인증·사설 CA·리포트와 Git 동작을 검사합니다.
실제 LLM 품질을 측정하는 테스트는 아닙니다.
실제 실행 검증 결과와 아직 확인하지 않은 조건은 저장소의 `docs/VALIDATION_STATUS.md`에 기록합니다.
