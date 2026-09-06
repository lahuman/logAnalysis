# Archify 도식 파일

사용자 설명은 [그림으로 보는 logAnalysis](../ARCHITECTURE.md)에 있습니다.

- `overview.html`: 전체 구성, Architecture.
- `event-processing.html`: 오류 한 건의 처리, Workflow.
- `offline-install.html`: 폐쇄망 반입·설치, Workflow.
- `*.architecture.json`, `*.workflow.json`: 수정 가능한 원본.
- `*.validation.json`, `*.delivery.json`: 스키마·구성·HTML 검사와 파일 체크섬.
- `*.visual-check.json`, `*.visual-check.*.png`, `*.visual-check.html`: 실제 브라우저 측정과 미리보기.
- `verification.json`: 최신 파일과 결합한 검증·시각 검토 기록.

HTML은 파일 하나만 복사해 브라우저에서 열 수 있습니다. 외부 폰트·CDN을 사용하지 않으며
브라우저에 설치된 시스템 글꼴을 사용합니다. 한국어가 표시되려면 열람 기기에 한국어 지원
글꼴이 있어야 합니다. 고정 메뉴·HTML 언어 속성은 Archify의 영어 기본값을 유지합니다.
PNG 미리보기는 HTML의 실제 화면을 캡처한 것으로, 애플리케이션 실행 화면은 아닙니다.

## 생성 도구와 변경 범위

도구: [tt-a1i/archify](https://github.com/tt-a1i/archify), `2.17.0-dev.1`.
참조 커밋: [`d8e4daf2610d512821365f41b139d874b29efe81`](https://github.com/tt-a1i/archify/tree/d8e4daf2610d512821365f41b139d874b29efe81).
상세 계약은 이 커밋의 [스킬 문서](https://github.com/tt-a1i/archify/blob/d8e4daf2610d512821365f41b139d874b29efe81/archify/SKILL.md)에 있습니다.

기본 템플릿에는 Google Fonts 연결이 포함되어 있어, 이번 결과물에는
[폐쇄망 글꼴 패치](archify-offline-fonts.patch)를 적용했습니다. 이 패치는 Google Fonts의
preconnect·stylesheet 링크를 제거하고 한국어 시스템 글꼴을 포함한 fallback을 사용합니다.
검증기·도식 스키마·연결 관계는 변경하지 않았습니다. 패치를 적용한 템플릿으로 공식
`validate`·`deliver`·`visual-check`를 다시 실행했으며, 완성 HTML을 사후 수정하지 않았습니다.

Archify는 전역 스킬로 설치하지 않았으며 개발용 복사본은 Git에서 제외되는
`build/archify-tool`에 있습니다. 도식 원본은 이 프로젝트의 코드와 문서를 기반으로 새로
작성했고, 배포하는 렌더러 코드의 MIT 고지는 [ARCHIFY-LICENSE.txt](ARCHIFY-LICENSE.txt)에 보존했습니다.

## 다시 생성하기

인터넷이 연결된 제작 환경에서 Node 18 이상과 Git을 준비합니다. 저장소 루트에서 실행합니다.
아래 내려받기·패치 과정은 새 도구 복사본에 한 번 적용합니다.

```bash
git clone https://github.com/tt-a1i/archify.git build/archify-tool
git -C build/archify-tool checkout d8e4daf2610d512821365f41b139d874b29efe81
git -C build/archify-tool apply ../../docs/diagrams/archify-offline-fonts.patch
```

생성 중 업데이트 확인도 끕니다. Linux에서는 `export ARCHIFY_UPDATE_CHECK_DISABLED=1`,
PowerShell에서는 `$env:ARCHIFY_UPDATE_CHECK_DISABLED='1'`을 사용합니다.

원본 JSON을 수정하고 검사한 다음 HTML을 확정합니다. 아래는 전체 구성도의 예입니다.

```bash
node build/archify-tool/archify/bin/archify.mjs validate architecture docs/diagrams/overview.architecture.json --quality showcase --json
node build/archify-tool/archify/bin/archify.mjs deliver architecture docs/diagrams/overview.architecture.json docs/diagrams/overview.html --quality showcase --json
node build/archify-tool/archify/bin/archify.mjs visual-check docs/diagrams/overview.html --json
```

다른 두 파일은 타입을 `workflow`로 바꾸고 해당 `.workflow.json`과 `.html`을 지정합니다.
`validate`·`deliver`의 JSON 출력은 대응하는 기록 파일에도 저장합니다. `visual-check`는
Chrome/Chromium이 필요하며 측정·이미지 파일을 직접 생성합니다. 오류가 있으면 원본을
고치고 다시 생성합니다. HTML을 직접 수정하면 생성·검증 체크섬과 어긋납니다.

자동 검사 성공 후 실제 이미지를 확인하고 `verification.json`의 시각 검토를 갱신합니다.
자동 검사의 `visualReview: pending`은 정상이며, 검토자가 실제로 확인한 범위는 별도 기록합니다.

## 현재 도식의 해석 범위

도식은 작업 디렉터리의 0.3.0 구현을 설명합니다. 공개 Git 커밋의 소스 검증 기능을 사용한
결과가 아니므로 노드에 Git 검증 배지를 넣지 않았습니다. 실제 DB·LLM 주소, 비밀번호,
내부 서비스의 운영 로그는 포함하지 않았습니다.

자동 브라우저 검사는 화면 넘침·기본 가독성·밝은/어두운 테마를 확인합니다. 이미지 검토는
기본 정적 화면의 선·글자·배치를 확인하며, Viewer의 모든 검색·내보내기 기능을 검증했다는
뜻은 아닙니다. 실제 Oracle 연결·내부 모델 품질 검증은 별도입니다.
