#!/bin/bash
# 봇을 재기동하고 멘션에서 응답까지 도는지 실제로 잰다 (sca-mxa).
#
#     tools/restart-and-probe.sh [봇이름...]
#
# preflight 게이트는 설정과 자격만 본다. 슬랙 이벤트를 받아 답까지 내는
# 경로는 기동이 성공해도 확인되지 않는다.
#
# launchd 나 run.sh 에 넣지 않았다. 점검 한 번이 엔진 호출 한 번과 테스트
# 채널 글 하나를 만들고, 슬랙 왕복이 기동 성공 판정에 들어가면 네트워크가
# 기동을 막는다. 반영한 사람이 부르는 명령으로 둔다.
#
# 식별자는 ASCII 로 둔다. macOS 의 bash 3.2 는 한글 변수 이름을 거부한다.
set -uo pipefail

LAUNCHCTL="${LAUNCHCTL:-/bin/launchctl}"
PROBE="${PROBE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/test-channel-probe.py}"
QUESTION="${RESTART_PROBE_TEXT:-지금 UTC 시각만 한 줄로 답해라}"
WARMUP="${RESTART_PROBE_WARMUP_SEC:-5}"

bots=("$@")
if [ ${#bots[@]} -eq 0 ]; then
  bots=(shinji rei asuka)
fi

failed=0
results=()
for bot in "${bots[@]}"; do
  for service in ingress worker; do
    "$LAUNCHCTL" kickstart -k "gui/$(id -u)/local.$bot.$service" >/dev/null 2>&1
  done
  # 접수기가 소켓을 다시 열기 전에 멘션을 넣으면 그 이벤트를 놓친다.
  sleep "$WARMUP"

  "$PROBE" "$bot" "$QUESTION" < /dev/null
  code=$?
  results+=("$bot : 종료코드 $code")
  [ "$code" -eq 0 ] || failed=1
done

echo "--- 재기동 뒤 점검 결과 ---"
for line in "${results[@]}"; do
  echo "$line"
done
exit "$failed"
