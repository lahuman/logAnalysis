# 구현 및 검증 현황

기준일: 2026-09-05. 현재 파일, 자동 테스트와 이전 구현 작업의 실행 결과를 기준으로
작성했다. 현재 버전은 `0.2.0`이다. 중요망 작업에서는 기존 Rocky Linux 9 VM에서
동봉 Python 3.11.8·Git으로 최신 테스트와 실제 압축파일의 오프라인 실행을 확인했다.
과거 ES·NIM 결과와 이번 합성 서버 검증을 구분해 기록한다.

후속 NIM·onprem 연동 구현을 반영했다. 최신 Windows 결과는 123개 중 118 통과·5 제외이며,
실제 NIM 합성 Java 분석과 리포트 생성도 성공했다. [NIM 연동 안내](NVIDIA_NIM.md)를 참고한다.

## 구현된 범위

| 영역 | 현재 동작 |
|---|---|
| 수집 | Elasticsearch 8.x 비동기 클라이언트, PIT·search_after·heartbeat, 고정 조회 구간, 중복 제거, 잘못된 문서 격리 |
| 파싱 | Java 예외 체인, suppressed·생략 프레임·module·inner class·default package, 구조화 프레임, 범용 fallback |
| 소스 조회 | 이벤트 commit 우선; commit이 없으면 로컬 ref SHA 고정, 오류 라인 blame과 해당 파일의 최근 patch 이력 조회 |
| 분석 | 정제·크기 제한한 Context, Responses HTTP 호출, 구조화 응답, 파일·라인 근거 필터링 |
| NIM 연동 | Chat Completions 어댑터, 출력 모드 선택, 공급자별 캐시 구분, 합성 Java smoke 도구 |
| 중요망 | onprem Chat Completions, 사설 CA, 선택적 인증, 명시적 HTTP 허용, 프록시 비사용 |
| 압축 배포 | Python 3.11.8·Linux 의존성 24개·로컬 Git 동봉, checksum/manifest, 오프라인 doctor, 설정 점검·smoke 실행기 |
| 상태 | SQLite 체크포인트·작업·분석 캐시·재시도·중단 복구·보존 정리 큐 |
| 출력·운영 | Markdown 리포트, CLI 상태 요약·종료 코드, POSIX 잠금, systemd service·timer와 credential 예시 |

패키지 버전은 `0.2.0`, 프롬프트 버전은 `java-incident-v2`, 분석기 버전은 `1`이다.
중요망 브랜치의 전체 작업·산출물 기준은 [브랜치 작업 정리](IMPORTANT_NETWORK.md)에 있다.
현재 리포트는 이벤트별로 생성하며 재발 시 기존 리포트를 갱신하지 않는다.
대표 리포트 갱신과 오류 그룹의 최초·최종 시각 집계는 후속 개선안이며 미구현이다.
상세 동작은 [중복 오류 처리](DUPLICATE_ERRORS.md)를 따른다.
비동기 Elasticsearch 실행에 필요한 `aiohttp>=3.9,<4`도 의존성에 포함되어 있다.

## 실행 결과

| 시점·환경 | 실행 결과 | 제외 또는 제한 |
|---|---|---|
| 2026-09-05 이전 구현 작업: Rocky Linux VM + 실제 Elasticsearch 8.19.21 | 99개 실행, 98 통과, 실패 0; `OK (skipped=1)` | Windows 전용 잠금 미지원 테스트 1개 제외 |
| 2026-09-05 이번 문서 현행화: Windows + Python 3.11.8 | 99개 실행, 94 통과, 실패 0; `OK (skipped=5)` | ES URL 미설정으로 통합 테스트 3개, POSIX 잠금 테스트 2개 제외 |
| 이번 Windows 확인: `python -m compileall -q src tests` | 통과 | 문법 컴파일 확인 |
| 이번 Windows 확인: `python -m pip check` | `No broken requirements found.` | 현재 가상환경 의존성 확인 |
| 2026-09-05 NIM 연동 후: Windows + Python 3.11.8 | 114개 실행, 109 통과, 실패 0; `OK (skipped=5)` | ES 3개·POSIX 잠금 2개 제외 |
| NIM 호스팅 API 실제 연결 | 모델 목록 확인, `nim_smoke_succeeded` 및 종료 코드 0 | 합성 Java 오류 1건; ES·Git·SQLite 경로는 제외 |
| GitHub 공개 준비 | 114개 중 109 통과·5 제외, compileall·pip check 통과 | 현재 Windows 환경에서 재확인 |
| 설치 패키지 | `0.1.0` wheel 빌드, 패키지 import·리포트 렌더링·README 메타데이터 확인 | 저장소 밖 템플릿이 없어도 기본 템플릿으로 렌더링 |
| 중요망 구현 후 Windows / Python 3.11.8 | 123개 실행, 118 통과·5 제외 | ES 3개·POSIX 잠금 2개 제외 |
| 중요망 구현 후 Rocky Linux 9 / 동봉 Python 3.11.8·Git | 123개 실행, 119 통과·4 제외 | ES 3개·Windows 전용 1개 제외; 기존 ES 서버는 이번 실행에서 가동하지 않음 |
| UBI 9 호환 빌드 컨테이너 | Python 3.11.8, 의존성 24개, Git 2.52.0 및 정적 zlib를 포함한 tar.gz 생성 | RHEL 9 x86_64, glibc 2.34 이상 대상 |
| 실제 압축파일 / 일반 사용자 / 외부 네트워크 격리 | 공백 경로 재배치, doctor, 설정 검사, HTTP 무인증·Bearer 인증, HTTPS 사설 CA, 리포트 3개, Git show/blame/log/cat-file, 변조 감지 통과 | 로컬 합성 Chat Completions 서버; 실제 LLM 추론 품질 검증은 아님 |

압축 배포 검증은 `unshare --net`으로 loopback만 남긴 공간에서 `nobody` 계정으로 수행했다.
기본 CA로 자체 서명 HTTPS 서버 연결이 실패하고, 지정한 CA로 연결하면 성공하는 것도 확인했다.
재현용 검증기는 [verify.py](../deploy/offline/verify.py), 사용자 절차는
[중요망 배포 안내](OFFLINE_DEPLOYMENT.md)에 있다. 빌드는 인터넷망에서 실행하고,
반입된 압축파일의 실행에는 pip·dnf·Docker·Podman을 사용하지 않는다.

공개할 문서의 로컬 링크와 TOML/JSON/Python 예시를 검증했다. 소스·문서와 wheel에
실제 credential이 포함되지 않은 것을 확인하고, 키·SSH 정보·로컬 설정·DB·전송
아카이브·개발용 런타임을 Git 업로드 대상에서 제외했다.

이전 Rocky Linux 작업에서도 `compileall`과 `pip check` 성공이 기록되어 있다.
Docker Compose 구성은 준비되어 있지만, 위 VM 테스트 결과를 Docker/WSL 실행
성공으로 해석하지 않는다.
공개 준비에서 테스트 이미지에 `test_deploy.py`가 읽는 배포 파일을 포함하도록
수정하고 정적 회귀 검사를 보완했다. 추가 mount는 필요하지 않으며 실제 Docker
실행은 후속 검증이 필요하다.

## 실제 Elasticsearch 통합 테스트

[통합 테스트 코드](../tests/integration/test_elasticsearch_live.py)는 다음 3개 메서드로 구성된다.

1. `test_live_es_paging_git_resolution_cache_and_reports`: PIT 페이지 조회, Git 소스,
   SQLite 완료 상태·캐시, 리포트, 재실행 중복 제거.
2. `test_live_es_without_git_commit_uses_head_blame_and_diff`: commit 없는 이벤트의
   ref SHA 고정, `repository_ref` 출처, blame·diff Context, Markdown 생성.
3. `test_internet_derived_java_errors_resolve_source_and_write_reports`: 아래 Java 오류
   7종을 실제 ES에 적재하고 임시 Git 소스 위치와 완료 리포트를 확인.

| fixture | 대표 원인 예외 |
|---|---|
| `null_pointer.txt` | `NullPointerException` |
| `stack_overflow_spring.txt` | `StackOverflowError` |
| `bean_creation_no_such_method.txt` | `NoSuchMethodError` |
| `out_of_memory.txt` | `OutOfMemoryError` |
| `sql_constraint.txt` | `SQLIntegrityConstraintViolationException` |
| `class_not_found.txt` | `ClassNotFoundException` |
| `completion_exception.txt` | `CompletionException` 내부의 `IllegalStateException` |

공개 사례를 축약·각색한 fixture이며, 출처는
[fixture 안내](../tests/fixtures/java/internet_samples/README.md)에 기록되어 있다.
소스는 위치 확인용으로 만든 임시 Java 파일이다. 실제 애플리케이션 장애를
재현하거나 원인 추론 품질을 평가하는 테스트는 아니다.

통합 테스트의 `CountingAnalyzer`는 고정된 결과를 반환하고 `root_causes`와
`recommended_fixes`는 빈 목록이다. 따라서 `COMPLETED` 리포트 생성은 파이프라인
연결 검증을 뜻하며, 실제 OpenAI 분석 품질 검증을 뜻하지 않는다. OpenAI HTTP
오류·재시도·응답 스키마 정책은 별도 mock 테스트로 확인한다.

## 남은 검증

| 항목 | 완료 기준 |
|---|---|
| OpenAI 실제 Responses smoke | 테스트 계정·선정 모델·비식별 로그로 실제 구조화 응답과 근거·리포트를 확인 |
| NVIDIA NIM 전체 배치 | smoke는 완료; 실제 ES·Git·NIM을 한 파이프라인으로 연결해 완료 상태·캐시·리포트 검증 |
| systemd 실제 운영 | 전용 사용자·credential로 service와 timer를 실행하고 2회 이상 주기, 중복 잠금, 종료 코드와 journal 확인 |
| RHEL 9 최종 호환 | 목표 Python 3.11.8, SELinux, 파일 권한, 내부 CA와 TLS를 실제 대상 환경에서 확인 |
| 실제 온프레미스 모델 | 제품·모델·내부 endpoint 선정 후 smoke 및 ES → Git → LLM 전체 배치를 실제 환경에서 확인 |
| 기관별 운영 조건 | SELinux·송신 ACL·스케줄러 및 FIPS 요구를 최종 서버에서 확인; 동봉 OpenSSL의 FIPS 적합성을 검증한 것은 아님 |
| 운영 Elasticsearch 계약 | 실제 버전·인덱스·필드 mapping·multiline 결합·조회 권한·지연 구간 확인 |
| 운영 소스·분석 품질 | 서비스별 로컬 ref 갱신 방식, source root·package, 배포 소스와 fallback 소스 차이, 전송 허용 범위와 결과 품질 확인 |

재현 절차는 [WSL/Docker 및 Linux 테스트](WSL_DOCKER_TESTING.md), 배포 준비는
[배포 안내](../deploy/README.md)를 따른다. `healthcheck`의 모델 조회 성공은
실제 Responses 생성·구조화 응답 검증을 대신하지 않는다.
