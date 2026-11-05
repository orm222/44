> 최신 Codespaces·Compose 추가분의 검증은 [CODESPACES_VALIDATION.md](CODESPACES_VALIDATION.md)를 보세요. 아래 내용은 기반 배포판의 이전 검증 기록입니다.
# Sol 6.1 · Terra 5.6 설정판 검증

검증일: 2026-11-05. 기준 ZIP: `codex_agent_harness_python39_160min.zip`.

## 설정과 보존 범위

공통 `model`은 `gpt-6.1-sol`, 독립 검토용 `reviewer_model`은 `gpt-5.6-terra`입니다.
질문 분석·요구사항·기능명세서·계획·구현은 Sol, 계획·작업·최종 검토와 사용자 설명 확인은 Terra를 사용합니다.
새 Windows 기본 실행기는 `codex_command: ["codex.exe"]`이며, 계정·전역 설정·개인 자격 증명을 변경하거나 포함하지 않습니다.

기존 Python 파일 **54개 전부 바이트 동일**합니다. 새 과제 9600초/기존 9000초 마감, 단계별 승인, 호출 예산, 입력 보호와 중단 복구 코드는 그대로입니다.
진행 중인 과제의 설정은 바꾸지 않고 원래 입력·호출 기록으로 재개해야 합니다.
다른 운영체제에서는 문제 접수 전에 `codex_command`를 해당 PC의 호환 CLI로 설정합니다.

## 이번 회귀검사

Python **3.9.25**, 관련 **53개 모두 통과**, 건너뜀·실패 0개입니다.
실행·증거 복구 26개, 기능명세/문서 승인 11개, 단계별 승인·준비·재개 7개, 160분/기존 마감 보존 9개를 다시 실행했습니다.
[모듈별 명령·결과·로그 해시](model-split-py39/summary.json)를 보존합니다.
전체 402개 결과는 코드가 동일한 직전 배포판의 검증 이력이며 이번에 전부 다시 실행했다고 표기하지 않습니다.
54개 파일의 Python 3.9 문법 컴파일, 압축 해제 후 CLI 도움말, ZIP CRC·전체 파일 SHA-256도 확인합니다.

## 실제 모델 호출

실제 Codex CLI로 작은 strict JSON 응답을 받아 결과·종료 코드 0·정리 완료를 검증했습니다.

| 역할 | 실제 모델 | 결과 |
|---|---|---|
| 지휘 | gpt-6.1-sol | 성공·동일 요청 재개 시 추가 호출 없음 |
| 구현 | gpt-6.1-sol | 성공·동일 요청 재개 시 추가 호출 없음 |
| 독립 검토 | gpt-5.6-terra | 성공·동일 요청 재개 시 추가 호출 없음 |

실측 기록: [지휘](sol-terra-live-coordinator.json), [구현](sol-terra-live-implementer.json), [검토](sol-terra-live-reviewer.json).
지휘 호출은 native CLI의 절대 경로로, 구현·검토 호출은 최종 `codex.exe` 설정으로 실행했습니다. 세 호출의 실제 실행 파일이 최종 설정으로 해석한 경로와 같음을 확인했습니다.

처음에는 PATH의 npm CLI **0.155.1**이 Sol 6.1을 `model_not_found`로 거절했습니다. 같은 계정에서 이미 설치된 앱 native CLI **0.166.0-alpha.5.1**은 Sol 호출에 성공했습니다.
새 설정이 이 PC의 native 실행기를 사용하도록 했으며, 기존 작업의 실행기 해석과 복구 기록에 영향을 주지 않도록 실행 코드를 변경하지 않았습니다.
[초기 실패 기록](sol-terra-initial-cli-failures.json)도 종료·정리를 확인한 뒤 새 시도로 검사했고 다른 모델로 자동 대체하지 않았습니다.

이번 실제 호출은 연결·모델 선택·구조화 응답·완료 기록 재사용 확인입니다. 전체 업무를 두 모델로 끝까지 해결하는 성능 평가는 아닙니다.
실제 Docker 빌드와 브라우저 렌더링은 이번 설정 변경에서 다시 실행하지 않았습니다. 관련 기존 필수 검증은 유지됩니다.

## 보존 기록

[직전 160분판 검증 이력](BEFORE_SOL_TERRA_HISTORY.md)의 402개 검사와 당시 Astra 호출은 과거 기록입니다.
직전 이력에 적힌 Windows 임시 파일 잠금의 최초 실패·단독 재통과 및 원인 미확정 한계도 그대로 보존합니다.
이번 배포 확인 정보는 [release-check](sol-terra-release-check.json), 최종 파일 해시는 최상위 `SHA256SUMS.json`입니다.
