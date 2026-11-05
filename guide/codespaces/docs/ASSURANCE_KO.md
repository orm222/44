# 환경·데이터·알고리즘·브라우저 추가 검사

이 기능은 기존 승인과 검사 흐름에 연결됩니다. 사용자에게 테스트 작성을 맡기지 않습니다. 지휘 에이전트가 문제에 맞는 계약을 작성하고, 구현자가 측정 코드를 만들며, Python이 실행 결과를 대조합니다. 아래 숫자는 **형식 설명용 예시**이며 실제 한계값은 담당자의 조건과 사용자가 승인한 계획으로 정해야 합니다.

## 실행 위치와 승인

```text
문서 확인 완료 → 환경 조사 → 계획 작성
  → 요구 환경·파일 연결 검사 → 계획 검토·사용자 승인
  → 승인한 실행 환경 준비 점검(미준비면 같은 단계에서 대기)
  → 작업 구현·테스트 → 해당 작업의 처리대장·후보 비교 → 작업 검토
  → 구현 결과 승인 → Docker 검사
  → 최종 처리대장·후보 비교 → 웹이면 실제 브라우저 검사
  → 최종 검토·내 설명 확인·최종 승인
```

새 계획의 `assurance`에는 다음 필드가 모두 필요합니다. 웹 여부와 각 검사의 적용·제외 이유를 명시합니다. `after_task`는 계획에 실제로 있는 작업 ID이며, 검사 실패 시 어느 작업을 다시 처리할지 연결합니다.

```json
{
  "project_type": "non_web",
  "reason": "웹 화면이 없는 명령형 프로그램",
  "environment": {"executables": [], "python_packages": [], "ports": []},
  "joins": [],
  "join_reason": "연결할 입력 파일 없음",
  "reconciliations": [],
  "reconciliation_reason": "행을 분류하는 기능 없음",
  "algorithms": [],
  "algorithm_reason": "최적화 후보 비교가 필요한 과제가 아님",
  "browser": null
}
```

검사 기준을 바꾸려면 기존 방향 전환 승인 절차를 사용합니다. 준비 점검을 위한 새 GATE는 추가하지 않습니다. 필수 검사를 시간 부족이나 환경 미준비 때문에 통과 처리하지 않습니다. 이전에 승인된 계획에 `assurance`가 없다면 이전 기능을 유지하며 추가 검사 **미설정**을 기록합니다.

새 계획은 기능 단위로 작성하며 중복 검사 명령은 작업별 범위로 나누거나 검증 계약을 보존해 통합하도록 검토합니다. 기존 승인 계획의 검사와 기완료 작업·최종 회귀검사는 줄이지 않습니다. 실패 요약은 `reason`뿐 아니라 `error`에 저장된 원인도 전달합니다.

## 1. 실행 환경

`environment`는 아래 요청 형태입니다. `python_packages`는 버전 조건 문자열이 아닌 배포 패키지 이름입니다.

```json
{"executables": ["python"], "python_packages": ["numpy"], "ports": [8765]}
```

최초 조사는 Python 실행 경로·버전, 설치 패키지 목록과 주요 실행기의 존재만 읽습니다. 상세 목록은 `.harness/environment.json`에 보존하고, 계획 모델에는 크기를 제한한 요약을 전달합니다. 계획에서 지정한 필수 실행기·패키지·포트가 준비되지 않으면 `BLOCKED`입니다. 포트는 지정한 번호에 대해서만 `127.0.0.1` 바인드 가능 여부를 확인하며, 예약을 보장하지 않습니다.

PLAN 승인 후 첫 테스트 작성(RED) 전에 승인한 환경을 다시 준비 점검합니다. `BLOCKED`이면 필요한 환경을 준비한 뒤 `py -3.9 run_harness.py develop`으로 같은 단계에서 재개합니다. 준비 대기도 과제의 원래 마감에 포함됩니다.

설치·업그레이드·원격 네트워크 접속은 하지 않습니다. 기본 조사 결과의 `DISCOVERED`/`PARTIAL`은 환경 정보를 읽었다는 뜻이며 필수 조건 통과가 아닙니다.

## 2. 입력 파일 연결

`joins`의 한 계약 예시입니다. 파일 경로는 `problem-project` 기준이며 UTF-8 CSV를 사용합니다.

```json
{
  "id": "stop-demand",
  "left": "data/demand.csv",
  "right": "data/stops.csv",
  "left_keys": ["stop_id"],
  "right_keys": ["stop_id"],
  "relationship": "many_to_one",
  "join_type": "left",
  "allow_unmatched": false,
  "allow_blank_keys": false,
  "expected_rows": null
}
```

관계는 `one_to_one`, `many_to_one`, `one_to_many`, `many_to_many`, 연결 방식은 `left` 또는 `inner`입니다. 빈 키와 미연결 허용 여부를 명시합니다. 예상 행 수를 승인했다면 정수를 넣고, 정하지 않았다면 `null`로 둡니다. 원본 ID 문자열을 보존하므로 `001`을 숫자 `1`로 자동 변환하거나 키를 임의 보정하지 않습니다.

현재 범위는 파일당 2 MiB·10,000행·64열, 전체 8 MiB이며 연결 계약은 최대 8개입니다. 상한을 넘는 입력을 일부만 읽고 통과했다고 하지 않습니다.

## 3. 입력 처리대장

`reconciliations` 항목은 계약과 실제 산출물 생성 명령을 묶습니다.

```json
{
  "after_task": "T-001",
  "contract": {
    "id": "input-ledger",
    "input": "data/demand.csv",
    "keys": ["request_id"],
    "parts": [
      {"file": "used.csv", "kind": "used"},
      {"file": "excluded.csv", "kind": "excluded"},
      {"file": "quarantined.csv", "kind": "quarantined"}
    ]
  },
  "probe": {
    "argv": ["python", "verify/write_ledger.py", "--problem", "{problem}", "--out", "{artifacts}"],
    "cwd": ".",
    "timeout_seconds": 120
  }
}
```

생성기는 실제 실행 결과에서 세 CSV를 작성합니다. 예를 들어 키가 `request_id`라면 각 대장에도 그 열이 있어야 합니다. 원본과 결과의 키 및 건수를 대조하여 누락·중복·알 수 없는 추가 ID를 찾습니다. 최적화 후 늘어난 경로 방문 행을 원본 요청 대장 대신 비교하지 않습니다. 제외 사유가 필요한 업무에서는 생성기가 사유 열을 함께 남기되, 하네스의 공통 대조는 선언된 키에 대해 수행합니다.

## 4. 알고리즘 후보 비교

`algorithms`의 한 항목 예시입니다. 후보는 최대 3개이며, 모든 사례에 모든 후보의 측정값이 한 번씩 있어야 합니다.

```json
{
  "after_task": "T-002",
  "contract": {
    "id": "route-quality",
    "direction": "minimize",
    "baseline": "baseline",
    "selected": "candidate",
    "candidates": ["baseline", "candidate"],
    "cases": [
      {"id": "small", "input": "data/small.csv", "exact": 8, "max_relative_gap": 0}
    ],
    "constraints": [{"name": "max_load", "min": 0, "max": 10}],
    "max_regression": 0
  },
  "measurements": "measurements.json",
  "probe": {
    "argv": ["python", "verify/compare_candidates.py", "--problem", "{problem}", "--out", "{artifacts}"],
    "cwd": ".",
    "timeout_seconds": 300
  }
}
```

측정 스크립트는 실제 입력으로 각 후보를 실행하고, 산출물 폴더의 `measurements.json`에 아래 형식으로 씁니다. `input_sha256`에는 각 후보가 읽은 **실제 입력 바이트의 SHA-256**을 넣습니다. 아래 문자열과 숫자는 형식 예시이며 실행 결과를 대신할 수 없습니다.

```json
{
  "results": [
    {"case_id": "small", "candidate_id": "baseline", "input_sha256": "실제 입력 파일 SHA-256", "objective": 10, "metrics": {"max_load": 8}},
    {"case_id": "small", "candidate_id": "candidate", "input_sha256": "실제 입력 파일 SHA-256", "objective": 8, "metrics": {"max_load": 8}}
  ]
}
```

- 필수 제약을 먼저 판정합니다. 선택안과 기준안은 모두 제약을 지켜야 합니다. 다른 후보가 제약을 어기면 순위에서 제외합니다. 목적값이 좋아도 위반을 숨기지 않습니다.
- `direction`은 `minimize` 또는 `maximize`입니다. `max_regression`은 기준안 대비 허용하는 **목적값의 절대 퇴보량**입니다. 비율이 아닙니다.
- `exact`는 승인된 작은 사례의 정확한 최적 목적값입니다. 모르면 `exact`와 `max_relative_gap`을 모두 `null`로 두고 정확한 최적성은 미측정으로 남깁니다.
- 상대 격차는 방향에 따른 최적값과의 거리 ÷ `abs(exact)`입니다. `exact=0`일 때 같으면 0, 다르면 유한한 상대 격차를 정할 수 없으므로 유한 격차 한계를 요구한 검사는 실패합니다. 정확한 최적값보다 더 좋다는 측정도 모순으로 처리합니다.
- 입력 해시 불일치, 빠진·중복된 사례/후보, 비유한 숫자, 빠진 제약 값, 임의의 `passed` 필드는 거부합니다. 제약 지표는 계약에 선언한 이름만 정확히 포함해야 합니다.
- 하네스는 승인된 지표와 측정값을 비교합니다. 업무별 목적함수·제약 계산이 맞는지는 측정 스크립트와 독립 기대값을 검토해야 하며, 일부 표본 통과를 전체 문제의 최적해 증명으로 표현하지 않습니다.

## 5. 실제 브라우저

`project_type`이 `web`이면 `browser`가 필요하며 `non_web`이면 `null`입니다. 예시는 작은 화면 한 개를 여는 설정입니다. 실제 문제에 필요한 대표 상태·화면 크기·조작을 승인된 사례에 넣습니다.

```json
{
  "after_task": "T-003",
  "config": {
    "browser": "chromium",
    "server": {
      "argv": ["python", "-m", "http.server", "8765", "--bind", "127.0.0.1"],
      "cwd": ".",
      "ready_url": "http://127.0.0.1:8765/",
      "timeout_seconds": 30
    },
    "cases": [
      {"id": "desktop", "path": "/", "width": 1280, "height": 720, "steps": [], "expect": [{"selector": "h1", "text": "앱 제목"}]}
    ]
  }
}
```

`steps`는 `{"action":"click","selector":"button.save","value":""}`처럼 `fill`, `click`, `select`, `press`와 선택자·값을 지정합니다. 기대값은 실제로 보여야 할 DOM 선택자와 텍스트입니다. 단순히 PNG를 저장했다는 이유만으로 사용 장면이 검증됐다고 하지 않습니다.

서버 주소는 명시적 포트를 가진 `127.0.0.1` 또는 `[::1]` 같은 루프백 IP를 사용합니다. 이미 사용 중인 포트를 재사용하지 않으며 브라우저의 다른 출처 요청은 차단합니다. 서버 자체가 외부 API를 호출하는 기능은 검사용 설정과 승인된 사례에서 별도로 다뤄야 합니다. 서버와 브라우저는 하네스가 띄운 것만 정리합니다. 상태 파일·임시 DB가 필요한 검사 서버는 `HARNESS_BROWSER_RUN_DIR` 환경변수의 폴더에 기록하도록 구성해야 합니다. 보호된 소스나 원본 업무 데이터를 바꾸면 중단합니다.

Playwright와 선택한 브라우저가 없거나 필요한 WebSocket 차단 API가 없는 버전이면 `BLOCKED`입니다. 자동 설치·OS 라이브러리 설치·tmux는 사용하지 않습니다. 비웹 과제에서는 Playwright를 불러오거나 브라우저를 실행하지 않습니다. 실제 브라우저 실행이 안 된 경우를 모의 검사 성공으로 대체하지 않습니다.

## 증거와 시간

`probe.argv`의 `{problem}`, `{app}`, `{artifacts}`는 각각 문제 폴더·개발 폴더·현재 실행의 산출물 폴더로 바뀝니다. 명령은 인수 배열로 실행하며, `cwd`는 개발 폴더 기준입니다. 산출물은 `{artifacts}` 안에만 작성하고 기존 입력·소스·다른 실행 기록은 바꾸지 않습니다.

각 측정 명령은 최대 300초이며 과제의 남은 제한 시간을 넘겨 실행하지 않습니다. 새 과제의 기본 제한은 160분(9600초)이고, 이미 9000초로 시작한 과제는 저장된 150분 마감을 유지합니다. 재개나 이번 버전 적용으로 시계를 다시 시작하지 않습니다. 후보 수를 늘리거나 필수 검사 범위를 변경하려면 계획을 다시 검토·승인합니다. 실행 계약·원본 해시·명령 종료·프로세스 정리·산출물 해시를 기록하므로, 과거 성공이나 손상된 보고서를 이번 통과 근거로 재사용하지 않습니다.

실제 배포판에서 실행한 테스트와 미실행 사항은 [검증 기록](../validation/REPORT.md)을 확인하세요.
