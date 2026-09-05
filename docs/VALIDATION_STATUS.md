# 구현 및 검증 현황

기준일: 2026-09-05. 현재 파일, 자동 테스트와 이전 구현 작업의 실행 결과를 기준으로
작성했다. 원격 VM 결과는 NIM 추가 전 작업 기록이며 이번 공개 준비에서 원격 실행을
반복하지 않았다. 현재 버전은 `0.1.0`이며 게시된 Git 이력으로 소스 버전을 식별한다.

후속 NIM 연동 구현을 반영했다. 최신 로컬 결과는 114개 중 109 통과·5 제외이며,
실제 NIM 합성 Java 분석과 리포트 생성도 성공했다. [NIM 연동 안내](NVIDIA_NIM.md)를 참고한다.

## 구현된 범위

| 영역 | 현재 동작 |
|---|---|
| 수집 | Elasticsearch 8.x 비동기 클라이언트, PIT·search_after·heartbeat, 고정 조회 구간, 중복 제거, 잘못된 문서 격리 |
| 파싱 | Java 예외 체인, suppressed·생략 프레임·module·inner class·default package, 구조화 프레임, 범용 fallback |
| 소스 조회 | 이벤트 commit 우선; commit이 없으면 로컬 ref SHA 고정, 오류 라인 blame과 해당 파일의 최근 patch 이력 조회 |
| 분석 | 정제·크기 제한한 Context, Responses HTTP 호출, 구조화 응답, 파일·라인 근거 필터링 |
| NIM 연동 | Chat Completions 어댑터, 출력 모드 선택, 공급자별 캐시 구분, 합성 Java smoke 도구 |
| 상태 | SQLite 체크포인트·작업·분석 캐시·재시도·중단 복구·보존 정리 큐 |
| 출력·운영 | Markdown 리포트, CLI 상태 요약·종료 코드, POSIX 잠금, systemd service·timer와 credential 예시 |

패키지 버전은 `0.1.0`, 프롬프트 버전은 `java-incident-v2`, 분석기 버전은 `1`이다.
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
| 운영 Elasticsearch 계약 | 실제 버전·인덱스·필드 mapping·multiline 결합·조회 권한·지연 구간 확인 |
| 운영 소스·분석 품질 | 서비스별 로컬 ref 갱신 방식, source root·package, 배포 소스와 fallback 소스 차이, 전송 허용 범위와 결과 품질 확인 |

재현 절차는 [WSL/Docker 및 Linux 테스트](WSL_DOCKER_TESTING.md), 배포 준비는
[배포 안내](../deploy/README.md)를 따른다. `healthcheck`의 모델 조회 성공은
실제 Responses 생성·구조화 응답 검증을 대신하지 않는다.
