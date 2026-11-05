# Codespaces용 웹·API·PostgreSQL 연결 예제

SvelteKit 화면에서 메모를 저장하면 FastAPI를 거쳐 PostgreSQL에 기록됩니다. 업무 기능을 완성한 앱이 아니라, 승인한 과제에 재사용할 연결 예제입니다.

```mermaid
flowchart LR
    A[브라우저·포트 3000] --> B[SvelteKit]
    B --> C[FastAPI·내부 포트 8000]
    C --> D[PostgreSQL·내부 포트 5432]
    D --> E[db_data 영속 볼륨]
```

## Codespaces에서 실행

ZIP의 내용이 저장소 루트에 오도록 올리세요. 루트에 .devcontainer와 run_harness.py가 있어야 합니다. GitHub에서 Codespace를 만들면 Python 3.9, Node 24, Codex CLI, 개발용 Docker 엔진을 준비합니다. GitHub 저장소나 Codespace 생성은 사용자가 합니다.

개발 환경은 Docker-in-Docker feature를 사용합니다. 이 feature는 개발 컨테이너에 privileged 권한을 사용하지만 앱의 web·api·db에는 그 권한을 주지 않습니다. Codex 로그인은 Codespace 안에서 별도로 합니다. 개발 환경 생성이나 아래 예제 실행은 하네스 과제 시간을 시작하지 않습니다.

저장소 루트의 터미널에서:

```bash
cd starters/web-postgres
python setup.py
docker compose up --build -d --wait
```

Ports 탭의 **3000**을 엽니다. 포트 공개 범위는 Private로 유지하세요. DB와 API의 포트는 호스트에 열지 않습니다. 3000이 사용 중이면 WEB_PORT=3001 docker compose up --build -d --wait로 실행하고 Ports에서 3001을 직접 전달하세요.

setup.py는 .secrets/db_password.txt를 한 번 생성하고 재실행해도 기존 비밀번호를 바꾸지 않습니다. 파일 값은 출력하지 않습니다. 비밀번호·DB 내용은 ZIP에 없습니다. Linux에서는 호스트의 .secrets 디렉터리 접근을 소유자로 제한하고, 컨테이너의 서로 다른 UID가 읽을 수 있도록 secret 파일은 읽기 전용으로 둡니다. 이 로컬 파일은 암호화된 비밀 저장소가 아닙니다.

## 실제 연결과 재시작 보존 확인

예제가 실행된 같은 폴더에서:

```bash
python api/check_stack.py --save-receipt .checks/note.json
docker compose down
docker compose up -d --wait
python api/check_stack.py --expect-receipt .checks/note.json
```

첫 검사는 웹 준비·실제 저장·조회 3개, 재시작 후 검사는 웹 준비·기존 값 조회 2개를 확인합니다. 성공하면 HARNESS_TEST_RESULT가 출력됩니다. 테스트용 메모 한 개를 DB에 실제로 추가합니다. 영수증 생성 직후 보존을 검사하세요(목록은 최신 100건입니다). 실패한 검사를 통과로 간주하지 않습니다.

종료도 **docker compose down**입니다. -v를 붙이면 DB가 삭제됩니다. 같은 폴더와 같은 Compose 프로젝트 이름으로 다시 실행해야 같은 볼륨을 사용합니다. init.sql은 처음 빈 DB를 만들 때만 실행되므로, 나중에 이 파일만 수정해도 기존 스키마가 바뀌지는 않습니다.

named volume은 일반 앱 종료·컨테이너 재생성을 견딥니다. **Codespace 삭제나 전체 개발 컨테이너 재빌드는 별개**입니다. 전체 재빌드가 Docker 볼륨을 정리할 수 있으므로 중요한 데이터는 먼저 덤프하고 Codespace 밖에 보관하세요. [GitHub 전체 재빌드 안내](https://docs.github.com/en/codespaces/developing-in-a-codespace/rebuilding-the-container-in-a-codespace)

```bash
mkdir -p backups
docker compose exec -T db pg_dump -U app_local -d app_local > backups/app_local.sql
```

backups 폴더는 Git에 올리지 않도록 제외했습니다. 필요한 덤프를 직접 다운로드하세요. app_local은 이 로컬 예제의 초기 관리자 계정입니다. 공개 서비스에 그대로 사용하지 말고 업무 인증·앱 역할·운영 백업을 별도로 계획합니다.

## 기존 하네스에 적용하는 지점

1. 새 과제는 저장소 루트에서 python run_harness.py start로 접수합니다. 이때부터 160분입니다.
2. problem-project/SPEC.md에 문제를 넣습니다. INTERVIEW.md에는 사용자가 선택한 웹 기본 구성이 기록되어 있습니다. 실제 업무 답변을 추가하고 python run_harness.py run을 실행합니다.
3. 질문 답변 → 요구사항 확인 → 기능명세서 확인은 기존대로 멈춥니다. 구현 계획에서 이 예제의 필요한 파일과 Docker 검사, secret·의존성 준비 방법을 명시하고 PLAN 승인을 받습니다.
4. 구현자는 승인된 범위만 app-project로 재작성·적용합니다. 예제 폴더를 자동 복사하거나, 예제 실행을 실제 과제 완료로 계산하지 않습니다. 데이터 모델과 테스트 기대값은 과제의 확정 명세로 바꿉니다.
5. 기존 Python 검사·독립 검토·Docker 검사·최종 승인·ZIP 생성을 따릅니다. 웹 과제의 필수 브라우저 검사는 별도이며 이 예제의 HTTP 검사로 대체하지 않습니다. Playwright와 브라우저는 승인된 환경 준비에서 설치해야 합니다.

예제 원본은 ../starters/web-postgres, 추가 지침은 ../.agents/skills의 두 SKILL.md에 있습니다(app-project에서 본 경로). 지휘·구현 모델이 읽어 참고할 수 있도록 INTERVIEW.md에 경로를 적었습니다. 지침 파일만 추가했다고 Python 실행기에 새 단계를 넣은 것은 아닙니다.

기존 Docker 계약을 구성할 때는 compose_file, service, checks를 사용합니다. 예제의 API 컨테이너 안에서 python check_stack.py --base-url http://web:3000을 실행하면 웹을 거쳐 저장·조회하는 3개 검사를 수행합니다. 새 앱에서는 준비 대기와 실제 업무 검사를 계약에 포함하세요. 하네스는 검사마다 고유 Compose 프로젝트를 사용하므로, 수동 예제 볼륨과 자동 검사용 볼륨은 서로 다릅니다.

secret 파일은 모델·검사가 실행되기 **전에** 준비해야 합니다. 승인된 과제에 옮길 때는 하네스가 허용하는 app-project 또는 problem-project 내부의 준비된 secret을 사용하도록 계획합니다. 실행 중 새 secret이나 의존성 파일을 소스에 생성하면 변경 감시가 중단시킬 수 있습니다. 새 과제 설정을 진행 중 과제 위에 덮어쓰지 마세요.

## 파일과 검증

| 파일 | 역할 |
|---|---|
| compose.yaml | 서비스 연결, 준비 검사, secret, DB 볼륨 |
| web/Dockerfile, web/src/routes | SvelteKit 빌드, 화면, 같은 출처 API 프록시 |
| api/Dockerfile, api/main.py | Python 3.9 FastAPI, SQL 바인딩, DB 준비 검사 |
| db/init.sql | 빈 DB의 초기 테이블 |
| setup.py | 재실행해도 비밀번호를 보존하는 로컬 준비 |
| api/check_stack.py | 실제 HTTP 저장·조회·재시작 보존 검사 |
| api/test_api.py | 입력 검증·SQL 바인딩·오류 응답 단위 테스트(DB는 mock) |

프런트 버전은 package.json과 package-lock.json에, Python 3.9 호환 API 버전은 requirements.txt에 고정했습니다. 의존성을 바꾸면 빌드와 실제 DB 검사를 다시 합니다.

로컬 단위 검사는 별도 Python 3.9 가상환경에 api/requirements-dev.txt를 설치한 뒤 api 폴더에서 python -m unittest test_api -v로 실행합니다. 프런트 검사는 web 폴더에서 npm ci, npm run check, npm run build 순서입니다.

이 배포 작업에서 실행한 검사와 실행하지 못한 항목은 루트 validation/CODESPACES_VALIDATION.md를 확인하세요. Docker 엔진이 없는 환경의 정적 검사 결과는 실제 컨테이너·볼륨 보존 성공을 뜻하지 않습니다.

구성 근거: [Compose 준비 순서](https://docs.docker.com/compose/how-tos/startup-order/), [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/), [PostgreSQL 이미지](https://hub.docker.com/_/postgres), [SvelteKit Node 배포](https://svelte.dev/docs/kit/adapter-node).
