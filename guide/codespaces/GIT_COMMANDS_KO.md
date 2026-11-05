# Git 명령어 모음과 설명

기존 Git 저장소 안에서 사용하는 안내다. 명령어 블록은 상황에 맞게 하나씩 실행한다.
명령이 실패하면 다음 단계로 넘어가기 전에 원인을 확인한다.
`main`, `origin`, `feature/login`, `fix/login-validation`, 파일 경로는 실제 프로젝트 값으로 바꾼다.
`git switch`는 Git 2.23 이상에서 사용할 수 있다.

## 1. 현재 상태 확인

| 명령어 | 설명 |
|---|---|
| `git status` | 수정 파일, 스테이징 상태, 충돌 여부 확인 |
| `git branch --show-current` | 현재 브랜치 이름 확인 |
| `git branch` | 로컬 브랜치 목록 확인 |
| `git branch -r` | 원격 추적 브랜치 목록 확인 |
| `git remote -v` | 원격 저장소 주소 확인. URL에 토큰이 있으면 공유하지 않기 |
| `git remote show origin` | 연결 가능할 때 원격 기본 브랜치 등의 정보 확인 |
| `git log --oneline --graph --decorate -15` | 최근 커밋과 브랜치 관계 확인 |
| `git diff` | 스테이징하지 않은 추적 파일 변경 확인 |
| `git diff --staged` | 다음 커밋에 들어갈 변경 확인 |

`git diff`에는 새로 만든 미추적 파일의 내용이 나오지 않는다. `git status`에서 확인하고 파일 내용도 직접 검토한다.

## 2. 처음 작업 브랜치 만들기

먼저 `git status`로 현재 수정 사항을 확인한다. 기존 작업이 남아 있으면 처리 방식을 정하고 진행한다.

```bash
git switch main
git pull --ff-only origin main
git switch -c feature/login
```

- `switch main`: 기본 브랜치로 이동한다.
- `pull --ff-only`: 원격 변경을 가져와 빨리 감기로만 반영한다. 로컬과 원격 이력이 갈라졌으면 멈춘다.
- `switch -c`: 현재 커밋에서 새 브랜치를 만들고 이동한다.

원격이 없는 로컬 저장소에서는 `pull`을 생략한다. 기본 브랜치가 `master`면 이름을 바꿔 실행한다.

## 3. 개발 중 변경을 커밋하기

코드를 수정한 뒤 변경에 맞는 검사를 실행한다. 예를 들어 unittest를 쓰고 `tests` 폴더가 있는 Python 프로젝트라면:

```bash
python -m unittest discover -s tests -v
```

테스트 방식과 경로는 프로젝트 설정을 따른다. 이 명령이 모든 프로젝트에 적용되는 것은 아니다.
실행 결과에서 발견된 테스트 수를 확인한다. `0 tests`이면 검증이 충분한지 다시 확인한다.

검사가 통과하면 관련 파일을 검토하고 커밋한다.

```bash
git diff
git diff --check
git add src/login.py tests/test_login.py
git diff --staged
git diff --staged --check
git commit -m "feat: 로그인 입력 검증 추가"
git status
```

`git add`는 커밋할 내용을 선택한다. `git commit`은 선택된 변경을 로컬 이력에 저장한다.
이미 다른 파일이 스테이징되어 있다면 이번 작업 파일만 커밋되도록 먼저 상태를 확인한다.
변경이 없으면 새 커밋을 억지로 만들지 않는다.

## 4. GitHub에 작업 브랜치 올리기

첫 푸시:

```bash
git push -u origin feature/login
```

같은 브랜치의 이후 푸시:

```bash
git push
```

`-u`는 로컬 브랜치가 따라갈 원격 브랜치를 설정한다. 커밋만 하면 로컬에 남고, 푸시해야 원격에 올라간다.
푸시가 거부되면 `git fetch origin` 후 이력을 확인한다. 강제 푸시로 덮어쓰지 않는다.

## 5. 병합하기 — 두 경로 중 하나 선택

### A. GitHub PR로 병합

1. 작업 브랜치를 푸시한다.
2. GitHub에서 기본 브랜치를 대상으로 Pull Request를 만든다.
3. 필요한 CI와 코드 검토를 통과한 뒤 Merge한다.
4. 로컬 기본 브랜치를 갱신하고 필요한 검사를 실행한다.

```bash
git switch main
git pull --ff-only origin main
```

PR로 병합을 끝냈다면 동일 작업을 로컬에서 다시 병합하지 않는다.

### B. 터미널에서 직접 병합

직접 병합·푸시가 허용된 저장소에서만 사용한다. 미커밋 변경이 없는 상태로 시작한다.

```bash
git switch main
git pull --ff-only origin main
git merge --no-edit feature/login
```

`merge`는 작업 브랜치의 변경을 현재 브랜치에 통합한다. 가능한 경우 빨리 감기로 끝나고, 이력이 갈라졌으면 병합 커밋을 만든다.
병합된 코드에서 필요한 검사를 실행하고 통과한 뒤 푸시한다.

```bash
git push origin main
```

검사가 실패하면 원인을 수정하고 다시 검증한 뒤 커밋한다. 해결되지 않은 결과를 푸시하지 않는다.
기본 브랜치 직접 푸시가 보호 규칙으로 거부되면 PR 경로를 따른다.

## 6. 병합 충돌 해결 또는 취소

충돌이 발생한 경우:

```bash
git status
git diff --name-only --diff-filter=U
```

출력된 파일을 열고 충돌 표시(`<<<<<<<`, `=======`, `>>>>>>>`)와 양쪽 변경을 확인한다.
원하는 최종 코드로 직접 수정하고 관련 검사를 실행한다. 이후 해결 파일을 추가하고 병합 커밋을 완료한다.

```bash
git add src/login.py
git diff --staged
git diff --staged --check
git commit -m "merge: 로그인 기능 병합 및 충돌 해결"
```

병합 후 필요한 검사를 실행하고 통과했을 때만 푸시한다.

병합을 포기하려면 병합이 아직 진행 중인 상태에서:

```bash
git merge --abort
```

이미 끝난 병합을 취소하는 명령은 아니다. 병합 전 미커밋 변경이 있었다면 완전한 복원이 어려울 수 있으므로 깨끗한 상태에서 병합을 시작한다.

## 7. 중간에 틀린 것을 발견했을 때

| 발견 시점 | 진행 방법 |
|---|---|
| 아직 커밋 전 | 현재 작업 브랜치에서 수정 → 검사 → 커밋 |
| 커밋했지만 병합 전 | 같은 작업 브랜치에서 수정 → 검사 → 추가 커밋 → 필요하면 푸시 |
| 이미 기본 브랜치에 병합한 뒤 | 최신 기본 브랜치에서 수정 브랜치 생성 → 수정·검사·커밋 → PR 또는 허용된 병합 |

병합 전 현재 작업 브랜치에서 수정했다면:

```bash
git add src/login.py tests/test_login.py
git diff --staged
git commit -m "fix: 빈 로그인 입력 처리 수정"
git push
```

위 커밋 전에 관련 검사를 실행한다. `git push`는 원격 추적 설정이 있고 푸시가 필요한 경우 사용한다.

병합 후 새 수정 작업을 시작한다면:

```bash
git switch main
git pull --ff-only origin main
git switch -c fix/login-validation
```

이후 수정 → 검사 → 커밋 → 푸시 → 병합 순서를 따른다. 같은 PR 브랜치를 푸시하면 열려 있는 PR에도 추가 커밋이 반영된다.

## 8. 자주 쓰는 보조 명령

| 명령어 | 용도와 조건 |
|---|---|
| `git switch feature/login` | 이미 있는 작업 브랜치로 이동 |
| `git fetch origin` | 원격 이력을 가져오되 현재 작업 브랜치에 자동 병합하지 않음 |
| `git diff main...feature/login` | 두 브랜치의 공통 조상부터 작업 브랜치까지의 변경 확인 |
| `git show HEAD` | 최신 커밋 내용 확인 |
| `git restore --staged src/login.py` | 초기 커밋 이후 저장소에서 스테이징만 취소. 작업 파일 내용은 유지 |
| `git stash push -u -m "로그인 작업 임시 저장"` | 추적 파일 변경과 미추적 파일 임시 보관. 무시된 파일은 제외 |
| `git stash list` | 임시 보관 목록 확인 |
| `git stash apply` | 가장 최근 보관 내용을 적용하고 보관 항목도 유지. 충돌 가능 |
| `git stash drop` | 가장 최근 보관 항목 삭제. 복원 내용 확인 후 사용 |
| `git revert 커밋해시` | 일반 커밋의 변경을 되돌리는 새 커밋 생성. 실제 해시로 바꾸기 |

`stash`는 장기 보관이나 백업을 대체하지 않는다. 에이전트는 기존 사용자 변경을 임의로 스태시하지 않는다.
`revert`는 작업 트리가 깨끗한 상태에서 사용한다. 병합 커밋을 되돌릴 때는 부모 선택과 이후 병합에 미치는 영향까지 별도 확인해야 한다.

## 9. 완료한 브랜치 정리 — 필요할 때만

먼저 병합과 원격 반영이 끝났는지 확인하고 기본 브랜치로 이동한다.

```bash
git switch main
git branch -d feature/login
git push origin --delete feature/login
```

- `branch -d`: 로컬 브랜치를 삭제하며, Git이 미병합으로 판단하면 거부할 수 있다.
- `push --delete`: 원격 브랜치를 삭제한다. 다른 사람이 사용하는 브랜치는 합의 없이 지우지 않는다.
- PR을 squash 방식으로 병합하면 로컬 `-d`가 거부될 수 있다. 실제 반영 여부를 확인하고 강제 삭제로 바로 넘기지 않는다.

## 10. 에이전트에 입력할 요청 예시

다음은 터미널 명령어가 아니라 Codex 등에 전달하는 작업 요청이다.

```text
AGENTS.md를 읽고 현재 Git 상태를 확인해.
새 작업이면 기본 브랜치에서 목적에 맞는 작업 브랜치를 만들어.
요구사항을 구현하고 필요한 검사를 실행한 뒤 관련 파일만 커밋해.
작업 브랜치를 푸시하고 PR을 만들어.
변경 내용, 실제 검사 결과, 커밋과 PR 주소를 알려줘.
병합은 이번 요청에 포함하지 않아.
```

병합까지 맡길 때는 마지막 문장을 다음으로 바꾼다.

```text
필수 검사와 저장소의 검토 조건을 충족하면 병합까지 진행해.
병합된 기본 브랜치의 검증 결과와 원격 반영 상태도 알려줘.
```

## 공식 참고 자료

- [Git switch](https://git-scm.com/docs/git-switch)
- [Git merge](https://git-scm.com/docs/git-merge)
- [Git revert](https://git-scm.com/docs/git-revert)

이 문서의 명령 흐름은 별도 로컬 실험 저장소에서 브랜치 생성, 커밋, 푸시, 병합, 충돌 해결·취소, 일반 커밋 되돌리기를 검증했다. 실제 프로젝트의 테스트·인증·GitHub 보호 규칙은 해당 프로젝트에서 확인해야 한다.
