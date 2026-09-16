#!/bin/bash
# 이미 있는 봇의 기동 wrapper 를 저장소 템플릿으로 다시 깐다.
#
#   tools/sync-run-sh.sh <이름> [<이름> ...]
#
# new-bot.sh 만 고치면 새로 만드는 봇만 보호된다. 이미 도는 봇들은 옛 판을
# 그대로 들고 있어, 차단 표식 같은 공통 동작이 그 봇들에는 없다 (sca-y4q).
#
# 이전 판은 run.sh.bak 으로 남긴다. 봇마다 손으로 고친 것이 있을 수 있어
# 덮기 전에 대조할 수 있어야 한다.
set -e
REPO=$(cd "$(dirname "$0")/.." && pwd)
[ $# -eq 0 ] && { sed -n '2,12p' "$0"; exit 2; }

for NAME in "$@"; do
  D="$HOME/.$NAME"
  [ -d "$D" ] || { echo "$NAME : 상태 디렉터리가 없다 : $D"; exit 1; }
  DISPLAY=$(python3 -c "
import json, sys
from pathlib import Path
p = Path('$REPO/profiles/$NAME.json')
print(json.loads(p.read_text(encoding='utf-8')).get('display_name', '$NAME') if p.is_file() else '$NAME')
")
  [ -f "$D/run.sh" ] && cp "$D/run.sh" "$D/run.sh.bak"
  sed -e "s|__NAME__|$NAME|g" -e "s|__DISPLAY__|$DISPLAY|g" -e "s|__REPO__|$REPO|g" \
    "$REPO/tools/templates/run.sh" > "$D/run.sh"
  chmod +x "$D/run.sh"
  echo "$NAME : 갱신했다. 이전 판은 $D/run.sh.bak"
done

echo
echo "반영하려면 재기동한다:"
for NAME in "$@"; do
  echo "  launchctl kickstart -k gui/\$(id -u)/local.$NAME.worker"
  echo "  launchctl kickstart -k gui/\$(id -u)/local.$NAME.ingress"
done
