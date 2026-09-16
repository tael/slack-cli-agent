#!/bin/bash
# 기동 차단 표식을 지우고 그 역할을 다시 띄운다.
#
#   tools/unblock.sh <이름> [worker|ingress]
#
# 역할을 안 주면 둘 다 본다. 설정 오류를 고친 뒤 이것을 부른다.
#
# 표식이 있으면 launchd 의 KeepAlive 조건이 거짓이라 스스로는 안 뜬다.
# kickstart 는 그 조건을 무시하므로 반드시 표식을 먼저 지운다 - 순서가
# 바뀌면 다시 78 로 끝나고 표식만 새로 생긴다.
set -e
NAME=$1
[ -z "$NAME" ] && { sed -n '2,9p' "$0"; exit 2; }
ROLES=${2:-"worker ingress"}
D="$HOME/.$NAME"

for ROLE in $ROLES; do
  MARK="$D/preflight-blocked/$ROLE"
  if [ -f "$MARK" ]; then
    echo "== $NAME.$ROLE 차단 사유"
    sed 's/^/   /' "$MARK"
    rm -f "$MARK"
  else
    echo "== $NAME.$ROLE : 표식 없음"
  fi
  # 실패하면 표식을 되돌리지 않고 그대로 실패한다. 조용히 복구한 척하면
  # 사람이 안 뜬 이유를 다시 찾아야 한다.
  launchctl kickstart -k "gui/$(id -u)/local.$NAME.$ROLE"
  echo "   다시 띄웠다"
done
