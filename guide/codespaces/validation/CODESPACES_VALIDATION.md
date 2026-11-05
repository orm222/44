# Codespaces·웹·PostgreSQL 추가 검증

검증일: 2026-11-05. 기반: codex_agent_harness_python39_sol_terra_160min.

## 변경 범위

- 기존 하네스 Python 파일 **54개는 원본과 바이트 단위로 동일**합니다.
- 추가: .devcontainer, PostgreSQL·Docker 스킬, starters/web-postgres 예제, 실행/검증 안내.
- 새 배포 기본 CLI는 Linux/Codespaces에 맞춰 codex로 지정했습니다.
- 실제 사용자 선택(SvelteKit + FastAPI + PostgreSQL)을 INTERVIEW.md에 기록했습니다.
- Sol 6.1 / 검토 Terra 5.6, 새 과제 9600초, 단계별 승인과 복구 로직은 유지합니다.
- 예제를 app-project에 자동 복사하거나 승인·필수 검사를 건너뛰는 코드는 넣지 않았습니다.

## 실행 결과

| 항목 | 결과 | 증거 |
|---|---|---|
| Python 3.9.25 구문 검사 | 58개 파일 통과 | codespaces-static.json |
| 기존 하네스 회귀 검사 | 50개 통과, 실패/오류/skip 0 | codespaces-harness-tests.txt |
| API 단위 검사 | 6개 통과 | codespaces-api-tests.txt |
| SvelteKit 타입 검사 | 오류 0, 경고 0 | 아래 명령·프런트 기록 |
| SvelteKit 프로덕션 빌드 | adapter-node 출력 생성, 종료 코드 0 | 아래 프런트 기록 |
| 실제 Node 프로덕션 서버 HTTP 검사 | 8개 통과 | codespaces-web-http.json |
| Compose 렌더링·기존 하네스 보호 규칙 | 통과 | codespaces-static.json |
| skills/assets Compose include 경로 | config --quiet 통과 | Compose v2.39.4-desktop.1 |
| secret 초기화 재실행 | 기존 값 보존 | codespaces-static.json |
| Docker 이미지 태그 | 4개 확인 | codespaces-image-manifests.json |

Python 회귀 검사는 tests의 test_deadline_160.py, test_development_recovery.py, test_development_checks.py를 실행했습니다. 최초 실행은 Windows의 기본 cp949 출력에서 em dash를 출력하지 못해 6개 오류·1개 실패가 발생했습니다. **코드는 변경하지 않고 Python -X utf8로 다시 실행해 50개 모두 통과**했습니다. 최초 로그도 codespaces-harness-tests-cp949-initial.txt로 남겼습니다. 이 배포의 주 환경인 Codespaces는 UTF-8이며, Windows 테스트 명령에서도 -X utf8을 사용하세요.

## 프런트 기록

Node 24.4.1에서 npm 설치 후 npm run check와 npm run build를 실행했습니다. 최종 고정 버전은 SvelteKit 2.70.3, adapter-node 5.5.7, Svelte 5.57.3, Vite 8.3.6입니다. 상세 의존성은 package-lock.json에 있습니다.

초기 SvelteKit 3.1.1에서는 기존 svelte.config.js를 허용하지 않아 실패했습니다. 예제와 호환되는 SvelteKit 2 계열로 고정한 뒤 검사와 빌드가 통과했습니다. SvelteKit이 Vite root를 덮어쓴다는 안내와 번들러 시간 안내는 빌드 오류가 아닙니다.

빌드 결과와 package.json/lock만 별도 디렉터리에 복사하고 npm ci --omit=dev --ignore-scripts로 production 의존성만 설치한 상태에서 node build를 실행했습니다. 개발용 node_modules 없이 서버가 실행됐습니다. API는 임시 HTTP 대역을 사용해 다음 8개를 검사했습니다.

1. 준비 상태 응답
2. 한국어 화면 HTML
3. POST 전달
4. GET 전달
5. 잘못된 출처의 non-JSON form POST를 프레임워크가 403으로 거부하고 저장하지 않음
6. upstream 422 상태 전달
7. check_stack.py의 영수증 저장·재조회
8. upstream 중단 시 503

이 검사는 **실제 PostgreSQL 통합·재시작 보존 검사나 실제 브라우저 검사에 해당하지 않습니다.** API 단위 검사도 DB 연결을 mock으로 대체합니다.

npm 설치 시 전체 의존성에서 low 6건이 보고됐습니다. production 의존성만 설치할 때는 0건이 보고됐지만, 이는 빌드에 포함된 모든 코드의 무결성·보안 검증을 뜻하지 않습니다. 호환성을 깨는 강제 업그레이드는 수행하지 않았습니다.

## 실행하지 못한 검증

이 PC에는 Docker CLI/Compose가 있으나 Docker 엔진 파이프가 없어 서버에 연결할 수 없었습니다. 따라서 아래는 **미검증**입니다.

- Dockerfile을 실제 Linux 엔진에서 빌드하고 세 서비스를 함께 기동
- 실제 PostgreSQL 쓰기·읽기·컨테이너 재생성 후 named volume 보존
- GitHub에서 Codespace 최초 생성과 포트 전달
- 새 Codespace의 Codex 로그인 및 Sol/Terra 모델 접근
- 실제 브라우저 조작 검사

이미지 태그 존재와 Compose 정적 통과는 위 검증을 대신하지 않습니다. starters/web-postgres/README.md에 실제 엔진에서 실행할 명령과 보존 확인 방법을 제공했습니다. 기존 validation의 모델 실호출 기록은 이전 환경의 기록이며 이번 Codespace 실호출 결과가 아닙니다.

## 재검사 명령

하네스 폴더 harness-kit/.harness에서 Python 3.9로 각 항목을 실행합니다.

```text
python -B -X utf8 -m unittest discover -s tests -p test_deadline_160.py -v
python -B -X utf8 -m unittest discover -s tests -p test_development_recovery.py -v
python -B -X utf8 -m unittest discover -s tests -p test_development_checks.py -v
```

API는 별도 3.9 가상환경에 requirements-dev.txt를 설치한 뒤 api 폴더에서 python -B -X utf8 -m unittest test_api -v. 프런트는 web 폴더에서 npm ci, npm run check, npm run build. 실제 세 서비스와 DB 보존 검사는 예제 README의 명령을 따릅니다.
