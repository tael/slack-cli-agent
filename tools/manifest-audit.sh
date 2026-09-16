#!/bin/bash
# 등록된 슬랙 앱 전체의 매니페스트를 정본과 대조하고, 이상이 있으면 소유자에게
# DM 으로 알린다. launchd 가 하루 한 번 부른다.
#
#   tools/manifest-audit.sh [<보내는봇>]
#
# 대조 도구는 sca-1v7 때 만들었지만 사람이 손으로 돌려야 했다. 값을 남기는
# 도구에는 그 값을 읽는 계기가 있어야 한다 (sca-4eo).
#
# 봇 기동 경로가 아니다. 실패해도 봇은 그대로 돈다 - 슬랙 장애로 조회가 안 되는
# 것과 설정이 어긋난 것을 여기서 구분할 수 없으므로, 판단은 사람이 한다.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BOT="${1:-shinji}"
LOG="$HOME/.$BOT/logs/manifest-audit.log"
mkdir -p "$(dirname "$LOG")"

OUT=$("$REPO/tools/slack-app.py" audit 2>&1)
CODE=$?
printf '%s  종료코드=%s\n%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$CODE" "$OUT" >> "$LOG"

# 이상 없음도 로그에 남긴다. 성공 경로가 조용하면 실패 한 줄이 그 뒤 구간
# 전체로 읽힌다.
[ "$CODE" -eq 0 ] && exit 0

"$REPO/tools/notify-owner.py" "$BOT" "슬랙 앱 설정이 정본과 다릅니다.

$OUT

반영하려면: tools/slack-app.py update tael <app_id> slack-apps/<이름>.json" >> "$LOG" 2>&1
exit 0
