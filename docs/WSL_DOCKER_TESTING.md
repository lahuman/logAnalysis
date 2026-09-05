# WSL 2 및 Docker 테스트

문서 기준일: 2026-09-05. Rocky Linux VM에서 실제 Elasticsearch 8.19.21과
99개 중 98개 통과한 이전 실행 기록이 있다. 이 결과는 Docker/WSL 실행 결과와
구분하며, 상세 내용은 [검증 현황](VALIDATION_STATUS.md)을 참고한다.

이 구성은 운영 배포 방식이 아니라 Linux 동작과 Elasticsearch 8.x 연동을
반복 검증하기 위한 개발 환경이다. Elasticsearch 보안 기능을 끈 테스트 전용
구성이므로 외부 네트워크에 노출하지 않는다.

## 준비 사항

- WSL 2 배포판
- Docker Desktop의 WSL 통합 또는 WSL 내부 Docker Engine
- Docker Compose v2
- Docker에 할당된 메모리 4GB 이상

오래된 WSL 배포판에서 systemd를 사용하려면 `/etc/wsl.conf`에 다음을
설정하고 Windows 터미널에서 `wsl --shutdown`을 실행한 뒤 다시 시작한다.

```ini
[boot]
systemd=true
```

권한과 파일 잠금 동작을 정확히 확인하려면 저장소를 `/mnt/c` 또는 `/mnt/e`
아래가 아닌 WSL의 Linux 파일 시스템(예: `~/src/logAnalysis`)에 clone한다.

## 전체 자동 테스트

Linux/WSL 셸의 저장소 루트에서 다음 명령을 실행한다. Compose는 테스트
Elasticsearch를 시작하고 healthcheck를 기다린 뒤 테스트를 실행한다.

```bash
docker compose build test
docker compose up --abort-on-container-exit --exit-code-from test test
docker compose down -v --remove-orphans
```

`Dockerfile.test`에 배포 검증이 읽는 `deploy/`, `Dockerfile.test`, `compose.yaml`을
포함했다. 추가 mount 없이 전체 테스트를 실행하도록 구성했으며 실제 Docker 실행
검증은 남아 있다. `.dockerignore`는 API 키·로컬 설정·SSH 정보·전송 아카이브를 제외한다.

테스트 이미지는 Python 3.11.8을 사용한다. 테스트 컨테이너는 일반 사용자로
실행되며, 실제 Elasticsearch를 대상으로 PIT paging, Git source resolution,
SQLite 상태, 분석 캐시 및 Markdown 생성을 함께 검증한다. Linux에서만 가능한
`flock` 경쟁과 비정상 종료 테스트도 전체 테스트에 포함된다.

NIM 추가 후 현재 테스트는 총 114개이며 ES 연동 가능한 Linux 환경에서는 Windows 전용
1개를 제외한 113개 통과가 기대값이다. 이 최신 스위트의 Linux 실행은 아직 미검증이며,
앞서 기록한 Rocky Linux 결과는 NIM 추가 전 99개 기준이다. 실제 ES 통합 테스트 3개는 기본 paging·캐시,
공개 사례 기반 Java 오류 7종, commit 없는 로그의 ref·blame·diff 경로를 검증한다.
Java 사례 수와 테스트 메서드 수는 다르다.

`docker compose down -v --remove-orphans`는 테스트 ES 볼륨까지 삭제한다.
테스트 실패 원인을 조사할 때는 아래 로그 확인 후 정리한다.

실패 후 컨테이너를 조사해야 하면 정리 전에 다음을 실행한다.

```bash
docker compose ps
docker compose logs --no-color elasticsearch test
curl http://127.0.0.1:19200
```

테스트 컨테이너를 조사하려면 `docker compose down`으로 정리하기 전에 로그를 확인한다.

## 기존 Linux 테스트 Elasticsearch 사용

Rocky Linux 등의 별도 테스트 VM에서는 Git, Python 3.11.8과 테스트 전용 ES를
준비하고 저장소 루트에서 실행한다. URL은 해당 테스트 환경의 주소로 바꾼다.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
LOG_ANALYZER_TEST_ES_URL=http://127.0.0.1:9200 \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pip check
```

Compose의 ES에 호스트에서 접근한다면 포트는 `19200`이다. 이 환경 변수는
통합 테스트에서만 읽으며 미설정 시 ES 테스트 3개가 제외된다. 테스트는 임시
인덱스를 생성·적재·삭제하므로 운영 클러스터를 대상으로 실행하지 않는다.

테스트 코드는 어댑터에 HTTP 주소를 직접 전달한다. 운영 CLI의 TOML 설정은
HTTPS 및 인증서 검증을 필수로 요구하므로 HTTP 테스트 ES를 같은 설정으로
`healthcheck`/`run`에 연결할 수 없다. CLI smoke에는 신뢰하는 CA가 구성된
HTTPS 테스트 ES와 OpenAI 테스트 자격증명을 준비한다.

## systemd 검증

Docker는 애플리케이션과 Elasticsearch 경계 검증에 사용하고, systemd service와
timer 자체는 systemd가 활성화된 WSL에서 검증한다.

먼저 unit 문법을 확인한다.

```bash
systemd-analyze verify deploy/log-analyzer.service deploy/log-analyzer.timer
```

[배포 안내](../deploy/README.md)에 따라 전용 사용자, 애플리케이션, 설정 및 unit 파일을 설치한
후 다음을 확인한다. 이 단계에는 테스트 Elasticsearch와 OpenAI 자격증명이
필요하다.

```bash
sudo systemctl daemon-reload
sudo systemctl start log-analyzer.service
sudo systemctl enable --now log-analyzer.timer
systemctl status log-analyzer.service log-analyzer.timer
systemctl list-timers log-analyzer.timer
journalctl -u log-analyzer.service --since today
```

기본 10분 주기로 두 번 이상 실행한 뒤 checkpoint와 중복 리포트가 없는지
확인한다. `deploy/hourly-schedule.conf.example`을 timer drop-in으로 적용한 뒤에는
`systemctl cat log-analyzer.timer`와 `systemctl list-timers`로 1시간 주기 변경을
확인한다.

## 범위와 최종 검증

- 이 Docker 테스트는 OpenAI 실계정 호출을 하지 않는다. API 오류 정책은 기존
  HTTP mock 테스트로 검증한다.
- OpenAI 실계정 smoke test에는 비식별 로그와 최소 Source Context만 사용한다.
- WSL의 성공 결과는 RHEL 9 호환 가능성을 높이지만 최종 승인을 대체하지 않는다.
  출시 전 실제 RHEL 9 대상 환경에서 systemd, 권한, SELinux 및 운영 TLS를 다시
  검증한다. 일반 애플리케이션 컨테이너 테스트만으로 host systemd·SELinux 검증을
  대체하지 않는다.
