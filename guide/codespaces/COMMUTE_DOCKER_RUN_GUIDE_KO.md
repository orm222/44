# 통근버스 풀이 ZIP 실행·재시험 안내

앞서 드린 통근버스 풀이 ZIP 기준으로, **‘최초 실행’과 ‘코드 수정 후 재시험’**을 나누면 됩니다. 둘 다 ZIP을 풀고 **`Dockerfile`이 있는 폴더의 터미널**에서 실행하세요.

## ① 처음 시작할 때 — 이미지 생성 → 테스트 → 웹서버 실행

아래 전체를 실행하세요. `&&`로 연결했으므로 앞 단계가 실패하면 다음 단계는 진행하지 않습니다.

```bash
docker info &&
docker build -t commute-demo:py39-final . &&
docker run --rm commute-demo:py39-final \
  python -B -m unittest discover -s tests -v &&
docker run -d \
  --name commute-demo-web \
  -p 127.0.0.1:8080:8080 \
  -v commute-demo-state:/state \
  commute-demo:py39-final \
  sh -c 'python cli.py run --db /state/commute.sqlite --input /data/bus_commute_bundle/candidate/data/full_v1/input.json --output /state/output && exec python -u server.py --host 0.0.0.0 --port 8080 --db /state/commute.sqlite'
```

이후 **Codespaces → 포트 → 8080 → 브라우저에서 열기**로 확인합니다.

## ② 중간에 오류를 고쳤을 때 — 이미지 재생성 → 재시험 → 실행 중인 컨테이너 교체

코드를 저장한 다음 아래 전체를 실행하세요.

```bash
docker build -t commute-demo:py39-final . &&
docker run --rm commute-demo:py39-final \
  python -B -m unittest discover -s tests -v &&
docker stop commute-demo-web &&
docker rm commute-demo-web &&
docker run -d \
  --name commute-demo-web \
  -p 127.0.0.1:8080:8080 \
  -v commute-demo-state:/state \
  commute-demo:py39-final \
  sh -c 'python cli.py run --db /state/commute.sqlite --input /data/bus_commute_bundle/candidate/data/full_v1/input.json --output /state/output && exec python -u server.py --host 0.0.0.0 --port 8080 --db /state/commute.sqlite'
```

이 명령은 **테스트가 통과해야 기존 컨테이너를 교체**합니다. 저장된 DB는 `commute-demo-state` 볼륨에 유지되고, 수정한 코드로 전체 데이터를 다시 계산합니다. 이후 기존 웹페이지를 새로고침하세요.

두 경우 모두 실행 로그는 다음으로 확인합니다.

```bash
docker logs --tail 30 commute-demo-web
```
