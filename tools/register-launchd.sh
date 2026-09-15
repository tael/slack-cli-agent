#!/bin/bash
# 봇을 launchd 에 등록해 상시 기동한다. ingress(슬랙 수신)와 worker(처리)를
# 따로 띄운다. 죽으면 다시 뜨고 로그인 시 자동으로 뜬다.
#
#   tools/register-launchd.sh <이름>
#
# 웹 콘솔은 여기서 안 띄운다. 콘솔은 모든 봇을 함께 보여주므로 봇에 매면
# 그 봇을 내릴 때 콘솔도 내려간다. tools/register-console.sh 를 쓴다.
set -e
NAME=$1
[ -z "$NAME" ] && { sed -n '2,7p' "$0"; exit 2; }
mkdir -p ~/Library/LaunchAgents "$HOME/.$NAME/logs"

gen() {
  local role=$1 extra=$2
  cat > ~/Library/LaunchAgents/local.$NAME.$role.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.$NAME.$role</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$HOME/.$NAME/run.sh</string><string>$role</string>$extra</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>WorkingDirectory</key><string>$HOME/.$NAME</string>
  <key>StandardOutPath</key><string>$HOME/.$NAME/logs/$role.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/.$NAME/logs/$role.err.log</string>
</dict>
</plist>
EOF
  launchctl bootout gui/$(id -u)/local.$NAME.$role 2>/dev/null || true
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.$NAME.$role.plist
}

gen ingress ""
gen worker ""

sleep 8
echo "== 상태"
launchctl list | grep "local.$NAME" || echo "  등록 실패"
echo
echo "두 번째 칸이 0 이 아니면 그 값이 마지막 종료 코드다. 로그를 본다:"
echo "  tail ~/.$NAME/logs/*.err.log"
