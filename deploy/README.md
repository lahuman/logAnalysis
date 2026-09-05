# RHEL 9 설치와 주기 실행

목표 환경은 RHEL 9 / Python 3.11.8입니다. Git과 해당 Python을 먼저 준비하세요.
아래 명령은 내려받은 `logAnalysis` 저장소 루트에서 실행합니다. 시스템의 기본 Python을
교체하지 않고 전용 가상환경을 사용합니다. 실제 RHEL 9의 systemd·SELinux 검증은
[검증 현황](../docs/VALIDATION_STATUS.md)의 후속 항목입니다.

개인 PC에서 먼저 실행해 보려면 [빠른 시작](../README.md)이나
[사용 가이드](../docs/USAGE.md)를 따르세요.

## 1. 실행 계정과 디렉터리

전용 계정이 없다면 생성합니다.

```bash
sudo useradd --system --user-group --home-dir /var/lib/log-analyzer --shell /usr/sbin/nologin log-analyzer
sudo install -d -m 0755 /opt/log-analyzer
sudo install -d -o root -g log-analyzer -m 0750 /etc/log-analyzer
sudo install -d -o root -g root -m 0700 /etc/log-analyzer/credentials
sudo install -d -o log-analyzer -g log-analyzer -m 0750 /var/lib/log-analyzer
sudo install -d -o log-analyzer -g log-analyzer -m 0750 /var/lib/log-analyzer/repos /var/lib/log-analyzer/reports
```

## 2. 애플리케이션 설치

```bash
python3.11 --version
sudo python3.11 -m venv /opt/log-analyzer/venv
sudo /opt/log-analyzer/venv/bin/python -m pip install .
```

패키지는 Python 3.11.x를 허용하지만 검증한 버전은 3.11.8입니다.

## 3. 서비스 소스와 설정

분석 대상 서비스의 Git 저장소를 `/var/lib/log-analyzer/repos` 아래에 준비합니다.
서비스 계정이 읽을 수 있어야 하며, 애플리케이션이 fetch나 checkout을 수행하지는 않습니다.
Git 원격 인증과 저장소 갱신은 별도로 관리합니다.

```bash
cp config/config.toml.example config/production.toml
```

`config/production.toml`에서 다음 값을 실제 환경에 맞게 편집합니다.

| 항목 | 지정할 값 |
|---|---|
| `[error_source]` | HTTPS ES 주소, 인덱스, 실제 CA 인증서 경로 |
| `[services.<name>]` | 로그의 `service.name`, Git 저장소 절대 경로, branch·source root·package |
| `[openai]` | [NIM 설정](../config/nvidia-nim.toml.example) 또는 사용할 OpenAI 모델 설정 |
| `run.lock_file` | `/run/log-analyzer/run.lock` |
| `state.path` | `/var/lib/log-analyzer/state.db` |
| `report.directory` | `/var/lib/log-analyzer/reports` |

NIM 예시는 `[openai]` 테이블 전체를 교체하는 데 사용합니다. 기본 OpenAI URL을 남기고
`provider`만 변경하지 마세요. 프롬프트 버전은 `java-incident-v2`입니다.

```bash
sudo install -o root -g log-analyzer -m 0640 config/production.toml /etc/log-analyzer/config.toml
```

## 4. 인증정보 설치

다음은 NVIDIA NIM + Elasticsearch API key 예시입니다. 준비한 로컬 파일에 키 값만
저장되어 있는지 확인한 뒤 설치합니다.

```bash
sudo install -o root -g root -m 0600 config/credentials/NVIDIA_API_KEY /etc/log-analyzer/credentials/nvidia_api_key
sudo install -o root -g root -m 0600 config/credentials/ES_API_KEY /etc/log-analyzer/credentials/es_api_key
sudo install -d -m 0755 /etc/systemd/system/log-analyzer.service.d
```

`/etc/systemd/system/log-analyzer.service.d/credentials.conf`를 다음 내용으로 만듭니다.

```ini
[Service]
LoadCredential=NVIDIA_API_KEY:/etc/log-analyzer/credentials/nvidia_api_key
LoadCredential=ES_API_KEY:/etc/log-analyzer/credentials/es_api_key
```

OpenAI를 사용하면 첫 줄을 `OPENAI_API_KEY`와 해당 키 파일 경로로 교체합니다.
ES 기본 인증이라면 `ES_API_KEY` 줄 대신 `ES_USERNAME`, `ES_PASSWORD` 두 credential을
지정합니다. [credential 예시](credentials.conf.example)를 참고하세요. 선택하지 않은
공급자의 credential 줄을 남기면 없는 파일 때문에 시작에 실패할 수 있습니다.

## 5. 연결 검사

아직 timer를 활성화하지 않은 상태에서 다음을 실행합니다. credential 이름은 앞서
선택한 공급자·ES 인증 방식과 맞춰야 합니다.

```bash
sudo systemd-run --wait --pipe --collect --unit=log-analyzer-healthcheck \
  --property=User=log-analyzer \
  --property=Group=log-analyzer \
  --property=WorkingDirectory=/opt/log-analyzer \
  --property=RuntimeDirectory=log-analyzer \
  --property=RuntimeDirectoryMode=0750 \
  --property=StateDirectory=log-analyzer \
  --property=StateDirectoryMode=0750 \
  --property=LoadCredential=NVIDIA_API_KEY:/etc/log-analyzer/credentials/nvidia_api_key \
  --property=LoadCredential=ES_API_KEY:/etc/log-analyzer/credentials/es_api_key \
  /opt/log-analyzer/venv/bin/python -m log_analyzer healthcheck \
  --config /etc/log-analyzer/config.toml
```

`healthcheck_succeeded`를 확인합니다. `run_skipped_locked`는 다른 실행이 잠금을
보유했다는 뜻입니다. NIM 모델 목록 조회만으로 생성 권한을 검증할 수 없으므로 실제
LLM 호출은 [NIM smoke](../docs/NVIDIA_NIM.md)로 먼저 확인하세요.

## 6. service와 timer 활성화

```bash
sudo install -m 0644 deploy/log-analyzer.service deploy/log-analyzer.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start log-analyzer.service
sudo systemctl enable --now log-analyzer.timer
systemctl status log-analyzer.service log-analyzer.timer
systemctl list-timers log-analyzer.timer
journalctl -u log-analyzer.service --since today
```

기본 실행은 매시 10분 단위이며 최대 30초의 무작위 지연이 있습니다. `Persistent=true`로
누락된 예약 실행을 보완합니다. oneshot service는 성공적으로 끝난 뒤 inactive 상태일 수
있습니다. 두 번 이상 실행하며 checkpoint, 중복 처리·캐시 수, 리포트와 실패 상태를
확인하세요. `run_completed.summary`가 배치 결과입니다.

1시간마다 실행하려면 기본 timer를 수정하는 대신 drop-in을 설치합니다.

```bash
sudo install -d -m 0755 /etc/systemd/system/log-analyzer.timer.d
sudo install -m 0644 deploy/hourly-schedule.conf.example /etc/systemd/system/log-analyzer.timer.d/schedule.conf
sudo systemctl daemon-reload
sudo systemctl restart log-analyzer.timer
systemctl list-timers log-analyzer.timer
```

## 7. 보존·업그레이드·문제 해결

기본 보존 기간은 30일입니다. 완전한 수집 구간을 처리한 뒤 오래된 종료 작업과 미사용
캐시를 정리하고, 참조가 없는 리포트를 삭제합니다. 삭제 실패는 다음 실행에서 재시도하며
진행 중 작업이나 아직 참조되는 리포트는 유지합니다.

업그레이드 전 timer를 멈추고 현재 배치가 끝날 때까지 기다립니다. 설정·설치 버전·리포트와
필요한 Git revision을 보존하고, SQLite backup을 사용하거나 모든 쓰기가 끝난 뒤 상태를
일관되게 백업합니다. 되돌릴 때는 호환되는 앱·설정·상태를 함께 복원하고 healthcheck 후
다시 timer를 활성화합니다. 과거 상태 복원으로 최신 이벤트가 다시 처리될 수 있습니다.
자동 롤백 명령은 없습니다.

종료 코드와 증상별 해결은 [문제 해결](../docs/TROUBLESHOOTING.md)을 참고하세요.
실행 계정·내부 CA·SELinux와 unit 제한은 대상 서버에서 확인해야 합니다.
