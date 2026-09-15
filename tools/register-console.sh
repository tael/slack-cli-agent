#!/bin/bash
# 웹 콘솔을 launchd 에 등록해 상시 기동한다.
#
# 콘솔은 모든 봇을 함께 보여주므로 특정 봇에 매지 않는다. 전에는
# local.asuka.web 으로 등록돼 있어서, 아스카를 내리면 콘솔도 같이
# 내려갔다(2026-09-15 에 고침). 상태 디렉터리와 venv 도 봇과 따로 둔다.
#
#   tools/register-console.sh [포트]
set -e
PORT=${1:-8787}
REPO=$(cd "$(dirname "$0")/.." && pwd)
D="$HOME/.console"
mkdir -p "$D/logs" ~/Library/LaunchAgents

if [ ! -x "$D/venv/bin/slack-cli-agent" ]; then
  python3 -m venv "$D/venv"
  "$D/venv/bin/pip" install -q -e "$REPO"
fi

cat > "$D/run.sh" <<EOF
#!/bin/bash
# 웹 콘솔 기동. 슬랙 토큰은 안 쓴다 - 파일만 읽고 쓴다.
set -e
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec "$D/venv/bin/slack-cli-agent" web --profile-dir "$REPO/profiles" "\$@"
EOF
chmod +x "$D/run.sh"

cat > ~/Library/LaunchAgents/local.console.web.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.console.web</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$D/run.sh</string><string>--port</string><string>$PORT</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>WorkingDirectory</key><string>$D</string>
  <key>StandardOutPath</key><string>$D/logs/web.out.log</string>
  <key>StandardErrorPath</key><string>$D/logs/web.err.log</string>
</dict>
</plist>
EOF

launchctl bootout gui/$(id -u)/local.console.web 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.console.web.plist
sleep 3
launchctl list | grep local.console.web || echo "  등록 실패"
echo "  http://127.0.0.1:$PORT/"
