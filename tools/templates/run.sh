#!/bin/bash
# __DISPLAY__ 봇 기동. launchd 가 이 스크립트를 부른다.
#
# 이 파일은 tools/templates/run.sh 에서 만들어졌다. 고칠 것이 있으면 저장소의
# 템플릿을 고치고 tools/new-bot.sh 나 tools/sync-run-sh.sh 로 다시 깐다.
# 여기를 직접 고치면 봇마다 갈린다 - 실제로 세 봇이 서로 달랐다 (sca-y4q).
set -e
D="$HOME/.__NAME__"
ROLE="$1"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# 토큰 자리는 봇마다 다르다. 옛 형태(.slack_*_token)를 쓰는 봇이 있고
# credentials.json 을 쓰는 봇이 있다. 있는 것만 넘기고 없으면 본체가
# 자격 파일에서 읽는다.
[ -f "$D/.slack_bot_token" ] && export SLACK_BOT_TOKEN=$(cat "$D/.slack_bot_token")
[ -f "$D/.slack_app_token" ] && export SLACK_APP_TOKEN=$(cat "$D/.slack_app_token")

# 설정 오류로 못 뜨는 상태를 알리는 종료코드. sysexits.h 의 EX_CONFIG 다.
# 운영 한계: 이 표식을 못 쓰면(디스크 부족, 권한) 차단이 안 걸려 재기동이
# 계속된다. 그 경우 사유를 stderr 에 남겨 로그에서 구분할 수 있게만 한다.
BLOCKED_EXIT=78

# exec 를 쓰지 않는다. exec 는 이 셸을 본체로 갈아치워, 종료코드를 보고
# 차단 표식을 남길 자리가 사라진다. 대신 자식으로 띄우고 기다린다.
"$D/venv/bin/slack-cli-agent" "$@" --profile __NAME__ --profile-dir __REPO__/profiles &
CHILD=$!

# launchd 의 SIGTERM 은 이 셸에만 온다. 넘기지 않으면 본체가 소켓을 쥔 채
# 살아남는다. exec 를 뺀 대가로 이것을 직접 해야 한다.
#
# 유예 뒤 KILL 까지 하는 이유 - 본체가 TERM 을 무시하면 아래 wait 가 영영
# 안 돌아오고, 그러면 launchd 는 이 서비스를 죽은 것으로도 산 것으로도
# 보지 못한다. 재기동도 정지도 안 되는 상태다 (코덱스 리뷰, 실측 확인).
SHUTDOWN_GRACE_SEC="${SHUTDOWN_GRACE_SEC:-20}"
_shutdown() {
  kill -TERM "$CHILD" 2>/dev/null || return 0
  ( sleep "$SHUTDOWN_GRACE_SEC"; kill -KILL "$CHILD" 2>/dev/null ) &
}
trap _shutdown TERM INT HUP

# set -e 아래에서 wait 가 0 이 아닌 값을 내면 셸이 즉시 끝나 아래로 못 간다.
set +e
wait "$CHILD"
CODE=$?
# trap 이 wait 를 깨웠으면 128+신호번호가 돌아온다. 자식이 실제로 끝난
# 코드를 받으려면 한 번 더 기다린다.
if [ "$CODE" -gt 128 ]; then
  wait "$CHILD" 2>/dev/null
  CODE=$?
fi
set -e

# 표식은 launchd 가 부르는 역할에만 남긴다. 사람이 `run.sh preflight` 로
# 상태를 보다 78 이 나온 것까지 표식으로 만들면, 점검했다는 이유로 봇이
# 안 뜬다.
case "$ROLE" in
  worker|ingress) ;;
  *) exit "$CODE" ;;
esac

if [ "$CODE" = "$BLOCKED_EXIT" ]; then
  if ! mkdir -p "$D/preflight-blocked" 2>/dev/null; then
    echo "차단 표식을 만들 자리를 확보하지 못했다 : $D/preflight-blocked - 재기동이 계속된다" >&2
    exit "$CODE"
  fi
  # 임시 파일에 쓰고 옮긴다. 반쯤 쓰다 만 표식을 launchd 가 보면 차단은
  # 되는데 사람이 원인을 못 읽는다.
  TMP="$D/preflight-blocked/.$ROLE.$$"
  {
    echo "시각      $(date '+%Y-%m-%d %H:%M:%S %z')"
    echo "역할      $ROLE"
    echo "종료코드  $CODE (EX_CONFIG - 설정 오류로 기동을 멈췄다)"
    echo "명령      $D/run.sh $*"
    echo "로그      $D/logs/$ROLE.err.log 에 사유가 있다"
    echo
    echo "고친 뒤 이 파일을 지우고 다시 띄운다:"
    echo "  $D/run.sh preflight"
    echo "  tools/unblock.sh __NAME__ $ROLE"
  } > "$TMP" 2>/dev/null && mv "$TMP" "$D/preflight-blocked/$ROLE" || {
    # 쓰기는 됐는데 mv 만 실패하면 임시 파일이 재기동마다 쌓인다 (코덱스 리뷰).
    rm -f "$TMP"
    echo "차단 표식을 쓰지 못했다 : $D/preflight-blocked/$ROLE - 재기동이 계속된다" >&2
  }
fi

exit "$CODE"
