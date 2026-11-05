---
name: docker-containers
description: 이 하네스의 승인된 앱에 Dockerfile과 Compose 실행·검증 구성을 적용한다. Codespaces 개발 환경과 결과 앱 컨테이너를 구분한다.
---

# 이 하네스의 Docker 적용

[실행 예제와 적용 안내](../../../starters/web-postgres/README.md)를 기준으로 필요한 부분을 재사용한다. 실제 Compose 원본은 [compose.yaml](../../../starters/web-postgres/compose.yaml) 한 곳이다. assets의 예제 경로는 이 원본을 include한다.

- .devcontainer는 하네스를 실행하는 개발 환경이다. 앱은 web → api → db의 별도 Compose 서비스다.
- 결과 앱의 언어·기능은 확정 명세와 계획을 따른다. 연결 확인용 메모 기능을 모든 과제에 넣지 않는다.
- web만 127.0.0.1:3000으로 열고 Codespaces Ports에서 연결한다. 브라우저는 같은 출처의 /api/notes를 호출하며 web 서버가 내부 api:8000으로 전달한다.
- DB 준비 상태와 초기 스키마까지 확인한다. depends_on만으로 데이터 보존이 검증된 것은 아니다.
- DB는 프로젝트 소유 named volume에 둔다. 고정 container_name, 외부 볼륨, 앱의 privileged·host network·Docker socket mount를 추가하지 않는다.
- 빌드 컨텍스트에서 .secrets, .env, 실행 기록을 제외한다. .dockerignore의 마지막 유효 줄은 .harness로 유지한다.
- 초기 secret 준비·의존성 설치는 실행 전에 계획한다. 모델·검사 실행 중 감시되는 소스를 바꾸지 않는다.
- Docker 검사는 실제 앱 경로를 통과하는 요청과 결과로 확인한다. config 통과·mock 테스트만으로 Docker 완료를 선언하지 않는다.
- 종료에는 docker compose down을 사용한다. 데이터 삭제 옵션, 전역 prune, 다른 프로젝트 정리는 별도 요청 없이 실행하지 않는다.
- Codespaces 전체 재빌드·삭제 전에 DB 덤프를 외부에 보관한다.

기존 계획 GATE와 구현·최종 GATE를 유지한다. 새 과제 160분과 진행 중 과제의 저장된 마감을 바꾸지 않는다. 다른 저장소의 flow 명령이나 하네스를 이식하지 않는다.
