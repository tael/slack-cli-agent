#!/bin/bash
# 매니페스트 일일 감사를 launchd 에 등록한다.
#
#   tools/register-manifest-audit.sh [<보내는봇>]
#
# 봇 기동과 무관한 별개 작업이다. 봇에 매면 그 봇을 내릴 때 감사도 사라진다.
set -e
BOT="${1:-shinji}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/local.manifest-audit.plist"
mkdir -p ~/Library/LaunchAgents "$HOME/.$BOT/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.manifest-audit</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$REPO/tools/manifest-audit.sh</string><string>$BOT</string></array>
  <!-- 하루 한 번. 슬랙 설정은 사람이 바꾸는 것이라 이보다 자주 볼 이유가 없고,
       자주 보면 차이 하나를 매일 여러 번 알리게 된다. -->
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StandardOutPath</key><string>$HOME/.$BOT/logs/manifest-audit.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/.$BOT/logs/manifest-audit.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$UID/local.manifest-audit" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
echo "local.manifest-audit 등록했다 - 매일 10:00, 보내는봇=$BOT"
