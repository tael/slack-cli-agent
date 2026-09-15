# 웨이브 4 계획 — 통합과 모듈 연결

기준 시점 640 passed(`core/ports.py` 포함). 웨이브 3 까지로 부품은 전부
생겼고 프로세스로 실행되지 않는 상태다. 이 웨이브가 그 연결을 만든다.

## 먼저 만든 것 (단독)

`core/ports.py` — `RequestHandler` Protocol 과 `HandleOutcome`. 워커와
파이프라인이 이 계약 하나로만 만난다. W4-E 와 W4-F 가 읽기만 한다.

## 단위 배분

같은 파일을 동시에 고치는 단위가 없어야 병렬이 성립한다.

| 단위 | 범위 | 새로 만드는 것 | 고치는 것 |
|---|---|---|---|
| W4-A | 슬랙 기록 조회 어댑터 | `slack/history_port.py` | 없음 |
| W4-B | 경과 시간 정규식 공용 이동 | `core/markers.py` | `guard/watch.py`, `slack/gate.py`, `slack/publisher.py` |
| W4-C | 답변 사후 점검의 엔진 호출 | `review/engine_adapter.py` | 없음 |
| W4-D | 이벤트 수신 프로세스 | `core/ingress.py` | 없음 |
| W4-E | 요청 처리 파이프라인 | `core/pipeline.py` | 없음 |
| W4-F | 큐 소비 워커 | `core/worker.py` | 없음 |

## W4-A 슬랙 기록 조회 어댑터

`reliability/ports.py` 의 `HistoryReader` Protocol 과 `slack/history.py` 의
실물 구현이 시그니처가 다르다. Protocol 은 `oldest: float` 를 받고 판정
불가면 `None` 을 돌려주며 `read_thread` 를 요구한다. 실물은 `oldest: str`
를 받고 판정 불가면 `HistoryUnavailable` 을 내며 `read_thread` 가 없다.

어댑터가 그 차이를 흡수한다. 실물을 고치지 않는다 — 실물은 원본 함수를
그대로 옮긴 것이고, 예외로 판정 불가를 표시하는 것이 그 계층에서는 맞다.

## W4-B 경과 시간 정규식 공용 이동

`slack/gate.py` 와 `slack/publisher.py` 가 `guard/watch.py` 에서 정규식을
가져온다. 슬랙 계층이 가드 계층에 의존할 이유가 없다. 두 정규식을
`core/markers.py` 로 옮기고 세 파일이 거기서 가져온다.

## W4-C 답변 사후 점검의 엔진 호출

`review/base.py` 의 `EngineCaller` Protocol 을 `EngineRunner` 위의 얇은
어댑터로 구현한다. `EngineRequest` 조립(workdir·model·effort·시스템
프롬프트)이 그 어댑터의 일이다.

## W4-D 이벤트 수신 프로세스

슬랙 이벤트를 받아 큐에 넣는 데까지만 한다. 엔진을 부르지 않는다.

## W4-E 요청 처리 파이프라인

`RequestHandler` 를 구현한다. 요청 하나를 세션 판정부터 발신까지 끝낸다.

## W4-F 큐 소비 워커

큐에서 작업을 집어 `RequestHandler` 에 넘기고 상태를 전이한다. 캐치업
대표건을 큐에 넣기 직전 대기 작업과 대조해 중복을 거른다.

## 웨이브 4 마무리 (단독)

`core/application.py` 조립과 CLI 기동 하위 명령. 위 6단위가 끝난 뒤.

## 공통 제약

- TDD. RED 를 실제로 실행해 관측한 뒤 구현한다. 실패 출력을 보고에 인용한다
- OOP. 공유 기본 구현이 있으면 ABC, 없으면 Protocol
- 조직 고유값을 넣지 않는다
- `git` 명령을 쓰지 않는다
- 테스트는 `python3 -m pytest -q` 로 실행한다. `-n 4` 는 `addopts` 에 있다
