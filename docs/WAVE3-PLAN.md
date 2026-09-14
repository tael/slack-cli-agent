# 웨이브 3 계획

기준 시점 443 passed. 각 단위는 서로 다른 디렉터리만 만진다 — 같은 파일을
동시에 고치는 단위가 없어야 병렬이 성립한다.

## 단위 배분

| 단위 | 범위 | 새로 만드는 것 | 읽기만 하는 것 |
|---|---|---|---|
| W3-A | 관리 명령 나머지 | `admin/channel_commands.py`, `admin/engine_commands.py` | `config/channel.py` |
| W3-B | 학습 제안·반영·되돌리기 | `learning/` | `config/paths.py` |
| W3-C | 답변 사후 점검 | `review/` | `engine/`, `render/` |
| W3-D | 진행 표시와 CLI | `observability/progress.py`, `cli.py` | 전부 |

## W3-A 관리 명령

원본 `handle_admin`(bot.py:3836)의 분기 중 아직 없는 것.

- 말수 3종 — 많게 `active`, 보통 `normal`, 적게 `quiet`
- 코치 모드 — `mode=agent_coach`, `mention_only`, `light_context` 를 함께 켠다
- api 모드 — `mode=api_helpdesk`
- 기본 모드 — `mode=private`
- 채널 해제 — 목록에서 제거. 없는 채널이면 그 사실을 답한다
- 엔진 승인 · 거부 — 전환 상태의 `approval` 을 바꾼다. 전환이 없으면 그렇다고 답한다

이미 있는 것은 도움말, 채널 목록, 엔진 상태다.

## W3-B 학습

원본 `show_proposal`, `apply_learning`, `revert_learning` 에 대응한다.
파일 쓰기는 `config/channel.py` 와 같은 임시 파일 교체 방식을 쓴다.

## W3-C 답변 사후 점검

원본의 부검·추적·서식 점검. `ReviewTask` 와 `reviews` 테이블을 쓴다.

## W3-D 진행 표시와 CLI

긴 작업의 중간 보고와 단일 진입점. 원본의 셸 스크립트 여러 개를 대체한다.

## 공통 제약

- TDD. RED 를 실제로 실행해 관측한 뒤 구현한다. 실패 출력을 보고에 인용한다
- OOP. 계약은 공유 기본 구현이 있으면 ABC, 없으면 Protocol
- 조직 고유값을 넣지 않는다. 회사 채널 ID·조직 이름·조직 전용 명령은 플러그인 몫이다
- `git` 명령을 쓰지 않는다
- 테스트는 `python3 -m pytest -q` 로 실행한다. `-n 4` 는 `addopts` 에 있다

## 웨이브 3 도중 병행한 정리

조직 고유값을 걷어냈다. 실제 슬랙 사용자 ID 와 채널 ID, 실명과 팀 이름이 주석과
테스트 픽스처에 남아 있었다. 예시값으로 바꾸되 "짧은 이름이 긴 이름의 접두사" 라는
관계는 유지했다 — 긴 이름부터 치환하는 동작을 검증하는 테스트가 그 관계에 의존한다.

주석의 회사 봇 이름 지칭도 일반 표현으로 바꿨다. `admin/` 과 `preflight/` 는
웨이브 3 단위가 동시에 만지고 있어 뺐다.

## 웨이브 3 완료 후 처리한 것

전부 처리했다. 조직 고유값 검사가 0건이다.

- 회사 봇 이름 지칭, 실명, 실제 슬랙 ID, 조직 전용 제품명과 도구 이름
- 원본 분석 문서의 파일명과 본문. 소스와 문서의 참조까지 함께 바꿨다
- 채널 목록의 사용자 대면 문자열

## 웨이브 4 (단독)

`core/application.py`, `core/ingress.py`, `core/worker.py` 통합. 처리할 것.

- `slack/gate.py` 와 `guard/watch.py` 의 의존 방향 정리. 경과 시간 안내 상수를 공용 위치로
- 되짚기 대표건을 큐에 넣기 직전 `JobQueue.pending()` 과 실행 중 여부를 대조해 거른다
- `CatchupService` 생성자에 실물 게이트웨이 값을 넘긴다
- `reliability/ports.py` 의 HistoryReader 대역을 `slack/history.py` 실물로 교체

## 최종 검증

조직 고유값 grep 0건, 워커 SIGKILL 후 재처리, 원본 대비 대조 불일치 0.
