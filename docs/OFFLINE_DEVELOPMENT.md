# RHEL 8.2에서 소스를 수정하고 바로 실행하기

0.5.0 배포본은 **RHEL 8.2 x86_64 / glibc 2.28 / Python 3.11.8**을 대상으로 합니다.
로컬 로그 파일·Oracle·ES 입력과 도식이 포함되어 있습니다.
애플리케이션의 Python 소스를 수정한 뒤 패키지 재설치나 배포본 재빌드 없이 실행할 수 있습니다.

## 처음 실행

```bash
sha256sum -c log-analyzer-0.5.0-rhel8-x86_64-python3.11.8.tar.gz.sha256
tar -xzf log-analyzer-0.5.0-rhel8-x86_64-python3.11.8.tar.gz
cd log-analyzer-0.5.0
./log-analyzer doctor
./log-analyzer test
```

`doctor`는 Python 3.11.8·glibc 2.28 이상·런타임 무결성·수정한 Python 파일의 문법을 검사합니다.
`test`는 동봉된 Python·라이브러리·Git으로 테스트를 실행합니다. 내부 LLM이나 실제 DB 설정은 필요하지
않습니다. 실제 DB 통합 테스트는 관련 `LOG_ANALYZER_TEST_*` 환경 변수를 명시한 경우에만 활성화됩니다.
실제 서버 연결을 원하지 않는 테스트 환경에는 이 변수들을 설정하지 마세요.

서버에 설치된 Python이나 Git을 바꿀 필요가 없습니다. 실행은 항상 배포 디렉터리의
`./log-analyzer` 명령을 사용하세요. RHEL 9용 이전 압축파일 위에 덮어 풀지 말고 새 디렉터리에 풉니다.

## 수정할 위치

| 경로 | 용도 | 수정 후 동작 |
|---|---|---|
| `src/log_analyzer/` | 도구의 실제 Python 코드 | 다음 명령 실행부터 반영 |
| `templates/` | 리포트 템플릿 | 다음 리포트 생성부터 반영 |
| `config/config.toml` | 로그·Git·LLM·상태·리포트 설정 | 다음 실행부터 반영 |
| `app/` | 동봉한 외부 Python 라이브러리 | 무결성 검사 대상 |
| `runtime/` | Python과 Git 바이너리 | 무결성 검사 대상 |

실행기는 **`src/`를 애플리케이션 코드로, `app/`을 외부 라이브러리로** 읽습니다.
애플리케이션 코드를 `app/log_analyzer`에 중복 복사하지 않으므로 수정할 코드가 하나뿐입니다.
`src/`, `templates/`, 운영 설정은 무결성 고정 대상에서 제외하지만 Python 문법 검사는 유지합니다.
문법 오류가 있으면 `doctor`가 파일 경로와 줄 번호를 표시합니다.
동작 자체의 오류는 테스트나 실제 실행으로 확인해야 합니다.

예를 들어 텍스트 로그 입력을 수정하려면:

```bash
cp -p src/log_analyzer/sources/file.py data/file.py.before-edit
vi src/log_analyzer/sources/file.py
./log-analyzer doctor
./log-analyzer test
./log-analyzer run
```

`run` 전에 [로컬 파일 입력 안내](LOCAL_FILES.md)에 따라 실제 로그·Git 저장소와 내부 LLM을 설정합니다.
`smoke`로 내부 LLM을 먼저 시험할 수도 있습니다. 실행기가 배포 디렉터리로 이동하므로 상대 경로도
그 디렉터리를 기준으로 해석합니다. 한 번의 실행이 끝나는 배치이므로 다음 실행부터 수정 코드가 로드됩니다.

## 수정 전후 결과 비교

이미 완료된 오류는 재실행해도 중복 처리로 건너뜁니다. 코드 수정만으로 과거 리포트가 다시 생성되지는
않으므로 별도 테스트 설정과 상태·리포트 경로를 사용하세요.

```bash
cp config/config.toml config/test.toml
vi config/test.toml
```

`config/test.toml`의 기존 두 항목을 다음처럼 바꿉니다.

```toml
[state]
path = "data/test-state.db"

[report]
directory = "data/test-reports"
retention_days = 30
```

```bash
./log-analyzer check-config --config config/test.toml
./log-analyzer run --config config/test.toml
```

다른 수정 버전과 비교할 때는 `test-state-v2.db`, `test-reports-v2`처럼 새 경로를 지정합니다.
운영 `data/state.db`와 리포트는 보존하세요. 원복하려면 백업한 소스 파일을 되돌리고 `doctor`와 `test`를
다시 실행하면 됩니다.

## RHEL 8 호환성을 유지하는 범위

RHEL 8 환경에서 Git을 빌드하고, 암호화 라이브러리도 glibc 2.28 호환 wheel로 묶습니다.
빌드 시 모든 ELF 파일의 요구 GLIBC 버전을 검사해 2.28보다 새로운 요구가 있으면 배포본 생성을 중단합니다.
관련 결과는 `manifest.json`의 `elf_compatibility`에 기록합니다.

기존에 포함된 라이브러리를 사용하는 Python 코드·템플릿 변경은 폐쇄망에서 바로 적용할 수 있습니다.
새 외부 라이브러리나 네이티브 확장 모듈을 추가하려면 인터넷 빌드 환경에서 RHEL 8 호환 의존성을
준비해 다시 묶어야 합니다. 서버의 glibc를 교체하거나 실행기의 버전 검사를 삭제할 필요는 없습니다.

glibc는 호환돼도 실제 서버의 내부 LLM API·Oracle DSN·CA·Git 저장소는 각 환경의 설정으로 검증해야 합니다.
배포 도식은 0.3.0 시점의 Oracle·ES 구성도이며, 로컬 파일과 소스 수정 방식은 이 문서와
[파일 입력 안내](LOCAL_FILES.md)가 현재 동작을 설명합니다.
