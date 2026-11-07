#!/bin/bash
# 봇을 launchd 에 등록해 상시 기동한다. ingress(슬랙 수신)와 worker(처리)를
# 따로 띄운다. 죽으면 다시 뜨고 로그인 시 자동으로 뜬다.
#
#   tools/register-launchd.sh <이름> [역할...]
#
# 역할을 안 주면 ingress 와 worker 를 등록한다. supervisor 는 재기동 요청
# 표식을 집어 재기동하는 감독이다(실행 명령은 slack-cli-agent supervise).
# 이미 도는 봇에 감독만 붙일 때는 역할로 supervisor 하나만 준다. 그러면 ingress 와 worker 는
# bootout 되지 않는다. 상태 디렉터리가 ~/.<이름> 이 아니면 STATE_DIR 로 준다.
#
# 웹 콘솔은 여기서 안 띄운다. 콘솔은 모든 봇을 함께 보여주므로 봇에 매면
# 그 봇을 내릴 때 콘솔도 내려간다. tools/register-console.sh 를 쓴다.
set -e
NAME=$1
[ -z "$NAME" ] && { sed -n '2,13p' "$0"; exit 2; }
shift
ROLES=${*:-ingress worker}
for role in $ROLES; do
  case "$role" in
    ingress|worker|supervisor) ;;
    *) echo "모르는 역할 : $role (ingress, worker, supervisor 중 하나)" >&2; exit 2 ;;
  esac
done
SD=${STATE_DIR:-$HOME/.$NAME}
mkdir -p ~/Library/LaunchAgents "$SD/logs"

gen() {
  local role=$1 extra=$2 cmd=$1
  # 역할 이름은 레이블과 로그 이름이고, run.sh 는 마지막 인자를 그대로
  # slack-cli-agent 명령으로 넘긴다. 감독의 명령 이름은 supervise 다.
  [ "$role" = supervisor ] && cmd=supervise
  cat > ~/Library/LaunchAgents/local.$NAME.$role.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.$NAME.$role</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$SD/run.sh</string><string>$cmd</string>$extra</array>
  <key>RunAtLoad</key><true/>
  <!-- KeepAlive 를 참으로 두면 설정 오류로 못 뜨는 봇이 ThrottleInterval
       주기로 영원히 재기동한다. 표식이 없을 때만 살려 두어 설정 오류는 한 번
       기록되고 멈추게 하고, 프로세스 충돌 같은 런타임 실패는 그대로 복구되게
       한다. 표식은 run.sh 가 종료코드 78 을 받았을 때 남긴다 (sca-y4q).
       표식을 만든 직후 한 번 더 뜨는 것은 정상이다 - launchd 의 경로 감시는
       경합에 취약하다고 man 5 launchd.plist 가 밝히고 있고, 실측으로도
       2회 기동 뒤 멈췄다. -->
  <key>KeepAlive</key>
  <dict>
    <key>PathState</key>
    <dict><key>$SD/preflight-blocked/$role</key><false/></dict>
  </dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- 기본값 20초가 지나면 launchd 가 KILL 한다. worker 는 TERM 뒤 진행 중 작업을
       최대 330초 기다리고 run.sh 는 345초 뒤 KILL 한다. 그보다 길게 둔다 (H67). -->
  <key>ExitTimeOut</key><integer>360</integer>
  <key>WorkingDirectory</key><string>$SD</string>
  <key>StandardOutPath</key><string>$SD/logs/$role.out.log</string>
  <key>StandardErrorPath</key><string>$SD/logs/$role.err.log</string>
</dict>
</plist>
EOF
  launchctl bootout gui/$(id -u)/local.$NAME.$role 2>/dev/null || true
  # 옛 차단 표식을 두고 재등록하면 방금 고친 설정으로도 안 뜬다. 지우는 것은
  # 그 역할을 bootstrap 하기 직전에 한다 - 둘을 미리 한꺼번에 지우면 재등록이
  # 중간에 실패했을 때 다른 역할이 의도치 않게 살아난다.
  rm -f "$SD/preflight-blocked/$role"
  # bootout 은 비동기다. 프로세스가 아직 남은 채 같은 레이블로 bootstrap 하면
  # "5: Input/output error" 로 실패하고, bootout 은 이미 끝났으므로 그 역할이
  # 도메인에서 사라진 채로 남는다. 2026-09-16 에 실제로 그랬다 (sca-fca).
  local tries=${BOOTOUT_WAIT_TRIES:-100}
  while [ "$tries" -gt 0 ] && launchctl print gui/$(id -u)/local.$NAME.$role >/dev/null 2>&1; do
    sleep "${BOOTOUT_POLL_SEC:-0.2}"
    tries=$((tries - 1))
  done
  if ! launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.$NAME.$role.plist; then
    echo "$NAME.$role : bootstrap 이 실패했다. 이 역할은 등록되지 않았다" >&2
    return 1
  fi
}

for role in $ROLES; do
  gen "$role" ""
done

# 등록 직후에는 아직 상태가 안 잡힌다. 시험은 이 대기가 필요 없어 줄인다.
sleep "${REGISTER_SETTLE_SEC:-8}"
echo "== 상태"
launchctl list | grep "local.$NAME" || echo "  등록 실패"
echo
echo "두 번째 칸이 0 이 아니면 그 값이 마지막 종료 코드다. 로그를 본다:"
echo "  tail $SD/logs/*.err.log"
