# NVIDIA NIM 연동

기준일: 2026-09-05. 기존 `[openai]` 설정 이름을 유지하면서
`provider = "nvidia_nim"`으로 NIM Chat Completions를 선택한다. SDK 추가 없이
기존 `httpx`, 정제·재시도·Pydantic 검증과 리포트 작성기를 사용한다.

## 선택한 호스팅 모델

- URL: `https://integrate.api.nvidia.com/v1`
- 모델: `nvidia/nemotron-3-super-120b-a12b`
- 초기 설정: timeout 120초, 출력 최대 4096토큰, 비스트리밍, `enable_thinking=false`
- 출력 형식: `json_schema`; 이 모델의 호스팅 endpoint에서 실제 생성 성공 확인

NVIDIA의 공개 `/models` 목록에서 모델 ID가 존재하는 것을 확인했다. 코드·추론 작업을
지원하는 모델이라 첫 Java 오류 분석용으로 선택했다. 모델 목록 조회는 인증 없이도
성공할 수 있어 키의 유효성이나 생성 권한을 증명하지 않는다.
[공식 모델 설명](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-super-120b-a12b),
[Chat Completions 규격](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-super-120b-a12b-infer).

## 실제 분석 smoke 실행

[NIM 설정 예시](../config/nvidia-nim.toml.example)는 `[openai]`만 포함한다.
아래 도구는 합성된 6줄 Java 소스와 `NullPointerException` 한 건을 전송하며
Elasticsearch, Git 저장소, SQLite 또는 배치 잠금을 사용하지 않는다. Windows에서도
실행할 수 있다. 운영 데이터나 저장소 소스는 이 명령으로 전송하지 않는다.

키는 `NVIDIA_API_KEY` 환경 변수 또는 `config/credentials/NVIDIA_API_KEY` 파일에
키 값만 저장한다. 해당 디렉터리는 `.gitignore`에 등록되어 있다. 환경 변수의 키가
파일보다 우선하므로 키를 교체한 경우 이전 환경 변수도 확인한다.

저장소 루트의 PowerShell:

```powershell
.venv/Scripts/python.exe -m log_analyzer.nim_smoke --config config/nvidia-nim.toml.example --credentials-directory config/credentials --output-directory reports/nim-smoke
```

Linux:

```bash
.venv/bin/python -m log_analyzer.nim_smoke \
  --config config/nvidia-nim.toml.example \
  --credentials-directory config/credentials \
  --output-directory reports/nim-smoke
```

성공 시 `nim_smoke_succeeded` JSON에 실제 생성된 Markdown 경로를 출력한다.
응답 스키마와 파일·라인 근거를 검사한 뒤, 근거가 있는 원인·수정안·검증 단계가
모두 있을 때만 성공 리포트를 쓴다. 정상 API 호출은 1회이며, 잘못된 JSON은 한 번만
복구 요청한다. smoke에서는 네트워크·HTTP 자동 재시도를 하지 않아 최대 2회 호출한다.

종료 코드는 성공 0, 분석 실패 1, 설정·키 누락 2, 파일 I/O 실패 3이다.
리포트의 `synthetic-fixture-not-a-deployment`는 테스트 표식이며 실제 배포 SHA가 아니다.

## 배치에 적용

[전체 설정 예시](../config/config.toml.example)의 `[openai]` 테이블을 NIM 예시의
테이블로 교체한다. `provider`만 바꾸고 이전 OpenAI URL이나 secret 이름을 남기지 않는다.
Elasticsearch·서비스·Git·상태·리포트 설정은 실제 Linux 환경 값으로 준비한다.

```toml
[openai]
provider = "nvidia_nim"
base_url = "https://integrate.api.nvidia.com/v1"
model = "nvidia/nemotron-3-super-120b-a12b"
api_key_secret = "NVIDIA_API_KEY"
timeout_seconds = 120
max_output_tokens = 4096
structured_output = "json_schema"
enable_thinking = false
```

`healthcheck`는 NIM의 `GET /models`에서 모델 ID를 확인한다. 실제 인증·생성·출력
형식은 위 smoke로 확인한다. 전체 배치의 `run`·`healthcheck`는 기존대로 POSIX 잠금이
필요하고, 모든 설정 URL은 HTTPS여야 한다.

systemd credential drop-in에서는 OpenAI credential 줄을 아래 줄로 **교체**한다.

```ini
LoadCredential=NVIDIA_API_KEY:/etc/log-analyzer/credentials/nvidia_api_key
```

Elasticsearch credential은 기존 인증 방식에 맞게 유지한다. 운영 설치는
[배포 안내](../deploy/README.md)를 따른다.

## 요청·응답 정책

`POST /chat/completions`에 system/user `messages`, `max_tokens`, `stream=false`를
전달한다. Responses 전용 `store`, `input`, `text.format`은 보내지 않는다. 따라서
OpenAI의 `store=false`와 동일한 공급자 보존 정책을 보장하는 것은 아니다.

| `structured_output` | 전송 방식 |
|---|---|
| `json_schema` | `response_format.type=json_schema`와 JSON Schema |
| `guided_json` | NIM의 `guided_json` 필드에 JSON Schema |
| `json_object` | JSON object 모드와 system 메시지의 스키마 지시; 서버 스키마 강제는 아님 |

모드별 지원은 NIM 모델·backend에 따라 다르다. 오류 시 자동으로 다른 모드나 공급자로
전환하지 않는다. 세 모드 모두 응답은 동일한 Pydantic 모델과 소스 근거 검증을 통과해야
한다. [NIM 구조화 출력 문서](https://docs.nvidia.com/nim/large-language-models/1.15.0/structured-generation.html).

`choices[0].message.content`의 JSON을 읽으며, 잘린 출력(`finish_reason=length`),
refusal·tool call·빈 응답은 성공 처리하지 않는다. 배치에서 429·주요 5xx·전송 실패는
기존 재시도 정책을 적용한다. 400·401·402·403·404·422는 설정·권한·모델 계약 문제로
배치를 중단하고 체크포인트를 전진하지 않는다.

공급자·URL·출력 모드·thinking 설정을 반영한 식별자를 내부 캐시의 분석기 버전에
추가해, 같은 모델 문자열이어도 다른 endpoint의 분석 결과와 섞이지 않게 했다.
공식 OpenAI의 기존 캐시 식별자는 유지한다.

## 현재 검증 결과

- Windows Python 3.11.8 자동 테스트: 114개 중 109 통과, ES 3개·POSIX 잠금 2개 제외.
- NIM 요청 형식, 정제, 스키마 복구, 근거 필터, 429 재시도, 인증 오류 중단,
  CLI 선택·모델 조회·캐시 구분과 smoke 리포트 생성을 mock 경계에서 확인.
- 실제 NVIDIA 모델 목록: HTTP 200, 선택 모델 ID 확인.
- 실제 생성 요청: 키 교체 후 `nim_smoke_succeeded`, 종료 코드 0.
  최초 키의 403 인증 실패는 교체 후 해소되었다.
- 결과 시각: `2026-09-05T06:48:24Z`. 모델은 `OrderService.java:4`의 null 참조를
  근거로 제시하고 null 검사 수정안과 3개 검증 단계를 반환했다.
- `json_schema`, `enable_thinking=false`, 출력 4096토큰 상한 설정으로
  Pydantic·소스 근거 검증과 실제 Markdown 저장이 완료되었다.
- 결과 파일: `reports/nim-smoke/nvidia-nim-smoke-nim-smoke-a7844602676148a29ede11f0b93727e5-a960a208c8d7.md`.
  생성물 디렉터리는 Git 제외 대상이며, 합성 입력의 공개 가능한 결과를
  [샘플 리포트](examples/nim-report.md)로 제공한다.
- 실제 ES → Git → NIM까지 연결한 전체 배치와 Linux/systemd 재검증은 별도 후속 작업이다.
