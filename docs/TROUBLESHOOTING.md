# 문제 해결

먼저 실행한 명령이 `nim_smoke`인지 전체 배치의 `run`/`healthcheck`인지 확인하세요.
샘플 분석은 LLM만 사용하고, 전체 배치는 ES·Git·상태 DB·POSIX 잠금도 필요합니다.

## 설치·설정

| 증상 | 확인과 해결 |
|---|---|
| `No module named log_analyzer` | 저장소 루트에서 `.venv/bin/python -m pip install .` 실행. Windows는 `.venv/Scripts/python.exe` 사용 |
| Python 버전 오류 | Python 3.11.x로 가상환경을 다시 준비. 목표·검증 버전은 3.11.8 |
| `configuration_invalid` / `nim_smoke_configuration_invalid` | TOML 중복 테이블·잘못된 키 확인. 전체 배치에 NIM 전용 축약 설정을 사용하지 않았는지 확인 |
| `ConfigError`로 NIM 샘플 실패 | API 키 파일명·경로·내용 확인. `NVIDIA_API_KEY.txt`가 아닌 `NVIDIA_API_KEY`이며 키만 저장 |
| Windows에서 `run_lock_unavailable` | 전체 배치는 Linux/WSL에서 실행. Windows는 `nim_smoke`와 단위 테스트 지원 |
| 출력·lock 디렉터리 권한 오류 | 실행 계정의 쓰기 권한 확인. 로컬 실험은 `state/`와 `reports/` 등 사용자 디렉터리 사용 |

## LLM 연결

| 증상 | 확인과 해결 |
|---|---|
| HTTP 401 / 403 | 공급자의 API 키·만료·계정 권한 확인. 키 교체 후 예전 환경 변수가 새 파일보다 우선하는지 확인 |
| HTTP 400 / 422 | 모델 ID, 출력 모드, 출력 토큰 상한 확인. `provider`만 바꾸고 이전 URL을 남기지 않았는지 확인 |
| HTTP 404 | `base_url`에 `/chat/completions`나 `/responses`까지 적지 않았는지 확인. 앱이 경로를 붙임 |
| HTTP 429 | 계정 한도·동시 요청 수 확인. 배치의 `run.max_concurrency`를 줄이고 다음 주기 확인 |
| timeout / 주요 5xx | 네트워크·모델 서비스 상태 확인. 배치는 자동 재시도하고 샘플 분석 도구는 호출을 종료 |
| `InvalidResponseError` | 출력 잘림·빈 내용·스키마 불일치 확인. NIM 모델의 지원 형식과 `max_output_tokens` 확인 |

NIM은 [공식 API 키 발급 안내](https://docs.api.nvidia.com/nim/docs/api-quickstart)를
따르세요. `/models` 조회가 성공해도 생성 권한이 유효하다는 뜻은 아닙니다.
출력 모드는 실패 시 자동으로 완화하지 않습니다. 모델에 맞게
[NIM 설정](NVIDIA_NIM.md)을 조정하고 샘플 오류로 먼저 확인하세요.

## 로그가 조회되지 않아요

1. `index` 패턴이 실제 인덱스를 포함하는지 확인합니다.
2. `@timestamp`와 시간대를 확인합니다. 처음에는 최근 60분에서 마지막 60초를 제외합니다.
3. `log.level`이 기본 필터 `ERROR` / `FATAL`과 일치하는지 확인합니다.
4. 이미 처리한 이벤트라면 `duplicate_events`가 증가할 수 있습니다.
5. 체크포인트가 있으면 최초 조회 범위 대신 저장된 시각부터 이어서 처리합니다.

## NO_SOURCE가 나와요

다음 순서로 확인합니다.

1. 로그의 `service.name`과 `[services.<name>]`이 같은가?
2. 전체 stack trace에 내 클래스·파일·라인이 있는가?
3. `application_packages`가 실제 애플리케이션 package와 일치하는가?
4. `source_roots` 아래에 해당 `.java` 파일과 일치하는 package 선언이 있는가?
5. 오류 라인 번호가 선택한 commit의 파일 범위 안에 있는가?
6. commit 없는 로그의 `branch`가 로컬 Git 저장소에서 해석되는가?
7. commit 있는 로그라면 그 commit이 로컬 heads/remotes/tags에서 도달 가능한가?

```bash
git --git-dir=repos/orders.git rev-parse main
git --git-dir=repos/orders.git show main:src/main/java/com/example/order/OrderService.java
```

위 경로는 내 저장소의 실제 값으로 바꿉니다. 파일 경로는 맞아도 소스와 로그의
버전이 다를 수 있습니다. Git 이력이 실제 배포 여부를 증명하지는 않습니다.

## 재시도·중복 실행·리포트

- `run_skipped_locked`: 같은 잠금을 보유한 실행이 있습니다. 기존 프로세스 상태를
  확인하세요. 실행 중인 프로세스를 무시하기 위해 lock 파일을 삭제하지 마세요.
- `RETRY_WAIT`: 정제된 요청을 저장했으며 다음 실행에서 기한이 된 작업을 처리합니다.
  계속 실패하면 배치의 종료 코드 1이 유지될 수 있습니다.
- `PERMANENT_FAILURE`: 입력이나 응답을 처리할 수 없거나 재시도 한도에 도달했습니다.
  자동 재실행만으로 이 종료 상태가 초기화되지는 않습니다.
- 원인·수정안의 근거 목록이 비어 있음: LLM이 제시한 파일·라인이 제공 범위를 벗어나
  필터링됐을 수 있습니다. 검증된 근거가 충분한지 사람이 확인하세요.
- 그룹 최초·최근 시각이 같음: 현재 리포트는 이벤트별 파일이며 해당 이벤트 시각을
  사용합니다. 그룹 전체 시간 집계는 구현하지 않았습니다.

## 전체 배치 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 정상 완료 또는 기존 잠금으로 실행 생략; `NO_SOURCE`만 있어도 0일 수 있음 |
| 1 | 재시도·영구 실패 잔여 작업, 리포트 정리 실패 또는 중단 |
| 2 | 설정·필수 credential 오류 |
| 3 | 잠금·저장소·외부 연결 등 인프라 오류 |
| 4 | 예상하지 못한 내부 오류 |

systemd에서는 `journalctl -u log-analyzer.service --since today`로 JSON 실행 요약을
확인합니다. 해결되지 않으면 [Issues](https://github.com/lahuman/logAnalysis/issues)에
버전·명령·종료 코드와 비식별 재현 예시를 남겨주세요. 비밀값과 운영 원문은 제거합니다.
