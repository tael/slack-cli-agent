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
#
# stdin 은 /dev/null 로 준다. 엔진 CLI(codex exec, claude -p)는 프롬프트를 인자로
# 받아도 stdin 을 먼저 읽어, 열린 stdin 을 물려받으면 EOF 를 못 받고 멈춘다
# (2026-09-28 codex exec 43분 공전). 비대화형 셸의 & 는 원래 /dev/null 을 주지만
# 그 규칙에 기대지 않고 적어 둔다 (H67).
"$D/venv/bin/slack-cli-agent" "$@" --profile __NAME__ --profile-dir __REPO__/profiles < /dev/null &
CHILD=$!

# 본체가 도는 동안 시스템 유휴 잠자기를 막는다. 마메치는 2026-09-15 AC 전원에서도
# Maintenance Sleep 으로 내려가 슬랙 소켓이 끊겼고 그 사이 멘션을 놓쳤다.
# -s 는 AC 전원일 때만 유효해 배터리에서는 평소처럼 잔다. 뚜껑을 닫아 생기는
# clamshell 잠자기는 막지 못한다 (pmset disablesleep, sudo 필요).
# caffeinate 로 본체를 감싸지 않고 -w 로 붙인다. 감싸면 CHILD 가 caffeinate 가 되어
# 아래 TERM 이 본체에 닿지 않는다. -w 는 본체 pid 가 끝나면 저절로 풀린다.
# caffeinate 가 없거나 실패해도 기동은 계속한다 (H67).
CAFFEINATE="${CAFFEINATE:-/usr/bin/caffeinate}"
if [ -x "$CAFFEINATE" ]; then
  "$CAFFEINATE" -s -w "$CHILD" < /dev/null > /dev/null 2>&1 &
fi

# launchd 의 SIGTERM 은 이 셸에만 온다. 넘기지 않으면 본체가 소켓을 쥔 채
# 살아남는다. exec 를 뺀 대가로 이것을 직접 해야 한다.
#
# 유예 뒤 KILL 까지 하는 이유 - 본체가 TERM 을 무시하면 아래 wait 가 영영
# 안 돌아오고, 그러면 launchd 는 이 서비스를 죽은 것으로도 산 것으로도
# 보지 못한다. 재기동도 정지도 안 되는 상태다 (코덱스 리뷰, 실측 확인).
#
# 유예는 worker 의 진행 중 작업 대기(RuntimeSettings.shutdown_grace_sec, 330초)
# 보다 길어야 한다. 짧으면 worker 가 턴을 마치려고 기다리는 중에 여기서 먼저
# 죽여, 그 턴이 큐에 걸린 채 남는다. 예전 20초가 그랬다 (H67). launchd 의
# ExitTimeOut 은 이보다 길게 둔다 (tools/register-launchd.sh).
SHUTDOWN_GRACE_SEC="${SHUTDOWN_GRACE_SEC:-345}"
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
#
# 첫 인자는 slack-cli-agent 명령 이름이다. 표식 이름은 launchd 역할 이름으로
# 맞춘다 - plist 의 PathState 와 tools/unblock.sh 가 역할 이름으로 보기
# 때문이다. 감독은 역할이 supervisor, 명령이 supervise 라 그대로 쓰면 표식이
# 감시하지 않는 자리에 생겨 10초마다 다시 뜬다 (H2 검토).
case "$ROLE" in
  worker|ingress) ;;
  supervise) ROLE=supervisor ;;
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
    echo "점검결과  $D/logs/$ROLE.out.log 에 어느 점검이 막았는지 있다"
    echo "오류      $D/logs/$ROLE.err.log"
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
