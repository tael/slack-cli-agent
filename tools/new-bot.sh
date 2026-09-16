#!/bin/bash
# 봇 하나를 만드는 절차 중 스크립트로 되는 부분. 브라우저가 필요한 단계는
# 안내만 하고 멈춘다. 절차 전체는 docs/에이전트-추가-입력정보.md 에 있다.
#
#   tools/new-bot.sh <ascii이름> <표시이름> <엔진> <모델> [아이콘경로]
#
# 조직 고유값은 환경변수로 받는다. 저장소에 박아 두면 다른 사람이 이 저장소를
# 써도 남의 소유자 ID 가 들어간 프로필이 만들어진다.
#
#   BOT_WORKSPACE            앱을 만들 슬랙 워크스페이스 이름
#   BOT_OWNER_USER_ID        소유자 슬랙 사용자 ID (U 로 시작)
#   BOT_TROUBLESHOOT_CHANNEL 문제를 알릴 채널 ID (C 로 시작)
#
# 기본값을 두지 않는다. 조직 고유값이라 스크립트가 임의로 정하면 안 된다.
# 후보를 모르면 tools/detect-defaults.py 가 지금 환경에서 찾아 보여준다.
#
# 예: tools/new-bot.sh rei "아야나미 레이" gemini gemini-3.8-flash ~/Jobs/tasks/bot-assets/rei-icon.png
set -e
NAME=$1; DISPLAY=$2; ENGINE=$3; MODEL=$4; ICON=$5
REPO=$(cd "$(dirname "$0")/.." && pwd)
# 아이콘을 안 주면 동봉한 기본 아이콘을 안내한다. 슬랙 앱 아이콘은 API 로 못
# 올려서 브라우저로 올리는 것까지는 사람이 한다.
[ -z "$ICON" ] && ICON="$REPO/src/slack_cli_agent/assets/default-bot-icon.png"
[ -z "$MODEL" ] && { sed -n '2,20p' "$0"; exit 2; }

for v in BOT_WORKSPACE BOT_OWNER_USER_ID BOT_TROUBLESHOOT_CHANNEL; do
  [ -n "${!v}" ] || {
    echo "환경변수 $v 가 없다. 이 값은 기본값을 둘 수 없다."
    echo "후보를 보려면: $REPO/tools/detect-defaults.py"
    exit 2
  }
done

case "$ENGINE" in
  claude) BIN=$HOME/.local/bin/claude ;;
  codex)  BIN=$HOME/.local/bin/codex ;;
  gemini) BIN=/opt/homebrew/bin/agy ;;
  *) echo "모르는 엔진: $ENGINE"; exit 2 ;;
esac

echo "== 1. 매니페스트"
# 정본 템플릿에서 이름 두 자리만 갈아 끼운다. 여기서 매니페스트를 새로 쓰면
# 저장소 정본과 갈린다 - 실제로 그래서 봇마다 설정이 달라졌고 세 봇의 DM 이
# 꺼진 채로 돌았다 (sca-1v7, sca-4zy). 치환은 AppManifest.from_template 가
# 하고 tests/unit/test_manifest.py 가 그 결과에 계약을 건다.
#
# venv 가 아니라 PYTHONPATH 로 부른다. 이 스크립트는 봇을 만들기 전에 도는
# 것이라 그 봇의 venv 가 아직 없고, manifest.py 는 표준 라이브러리만 쓴다.
PYTHONPATH="$REPO/src" python3 - "$NAME" "$DISPLAY" "$REPO/slack-apps/_template.json" > /tmp/$NAME-manifest.json <<'PY'
import json, sys
from pathlib import Path

from slack_cli_agent.slack.manifest import AppManifest

name, display, template = sys.argv[1], sys.argv[2], sys.argv[3]
manifest = AppManifest.from_template(Path(template), name=name, display=display)
print(json.dumps(manifest.raw, ensure_ascii=False))
PY

echo "== 2. 매니페스트 점검"
"$REPO/tools/slack-app.py" validate "$BOT_WORKSPACE" /tmp/$NAME-manifest.json

echo "== 3. 앱 생성"
APP_ID=$("$REPO/tools/slack-app.py" create "$BOT_WORKSPACE" /tmp/$NAME-manifest.json)
echo "  app_id=$APP_ID"

echo "== 4. 아이콘"
"$REPO/tools/slack-app.py" icon "$BOT_WORKSPACE" "$APP_ID" "$ICON"

echo "== 5. 상태 디렉터리"
D="$HOME/.$NAME"
mkdir -p "$D/persona/knowledge" "$D/prompts" "$D/logs" "/Users/Shared/$NAME-work"
case "$ENGINE" in
  gemini) mkdir -p "$D/engine/gemini-home/.gemini/antigravity-cli"
          cp ~/.gemini/antigravity-cli/antigravity-oauth-token "$D/engine/gemini-home/.gemini/antigravity-cli/" 2>/dev/null || true
          chmod 600 "$D/engine/gemini-home/.gemini/antigravity-cli/antigravity-oauth-token" 2>/dev/null || true ;;
  codex)  mkdir -p "$D/engine/codex-home"
          cp ~/.codex/auth.json "$D/engine/codex-home/" 2>/dev/null || echo "  ! codex auth.json 을 손으로 복사해야 한다" ;;
  claude) mkdir -p "$D/engine/claude-home" ;;
esac

# 기동 wrapper 는 저장소 템플릿에서 만든다. 여기서 따로 쓰면 봇마다 갈리고,
# 그러면 차단 표식 같은 공통 동작이 일부 봇에만 들어간다 (sca-y4q).
sed -e "s|__NAME__|$NAME|g" -e "s|__DISPLAY__|$DISPLAY|g" -e "s|__REPO__|$REPO|g" \
  "$REPO/tools/templates/run.sh" > "$D/run.sh"
chmod +x "$D/run.sh"

cat > "$D/persona/PERSONA.md" <<EOF
# 정체

나는 $DISPLAY 다. 슬랙에서 조사와 작업을 맡는 에이전트다.

이름을 물으면 "$DISPLAY 입니다" 라고 답한다. 엔진 이름이나 모델 이름을
자기 이름으로 말하지 않는다.

# 말투

- 존댓말을 쓴다. 군더더기 없이 짧게 답한다
- 비유와 의인화를 쓰지 않는다
- 확인하지 않은 것을 확인한 것처럼 말하지 않는다. 모르면 모른다고 한다
EOF

echo "== 6. 프로필"
python3 - "$NAME" "$DISPLAY" "$ENGINE" "$MODEL" "$BIN" "$REPO" \
         "$BOT_OWNER_USER_ID" "$BOT_TROUBLESHOOT_CHANNEL" <<'PY'
import json, sys
name, display, engine, model, binary, repo, owner, channel = sys.argv[1:9]
# claude 는 봇별 홈이 없다. 적으면 프로필 적재가 거부된다.
if engine != "claude":
    engine_home = {"home_dir": f"~/.{name}/engine/{engine}-home"}
else:
    engine_home = {}
data = {
  "name": name, "display_name": display,
  "primary_engine": {"type": engine, "binary": binary, "model": model,
                     "model_owner": model, **engine_home},
  "state_dir": f"~/.{name}", "work_root": f"/Users/Shared/{name}-work",
  "launch_label": f"local.{name}", "owner_user_id": owner,
  "troubleshoot_channel": channel, "plugins": [],
  "settings": {"request_timeout_sec": 900, "max_concurrent": 4,
               "base_tools": ["Read", "Grep", "Glob"],
               "owner_tools": ["Write", "Edit", "Bash", "NotebookEdit", "WebFetch", "WebSearch"]},
}
open(f"{repo}/profiles/{name}.json", "w", encoding="utf-8").write(
    json.dumps(data, ensure_ascii=False, indent=2) + "\n")
PY

echo "== 7. venv"
python3 -m venv "$D/venv"
"$D/venv/bin/pip" -q install -e "$REPO"

echo
echo "여기까지 자동이다. 아이콘도 올라갔다. 남은 것은 브라우저로 한다."
echo "  1) 설치 승인   https://api.slack.com/apps/$APP_ID/install-on-team"
echo "     설치 후 OAuth 페이지에서 봇 토큰(xoxb)을 복사한다"
echo "  2) 앱 토큰     https://api.slack.com/apps/$APP_ID/general"
echo "     Generate Token and Scopes, 스코프는 connections:write"
echo "  3) 두 토큰을 $D/credentials.json 에 넣고 chmod 600"
echo "     {\"bot_token\": \"xoxb-...\", \"app_token\": \"xapp-...\"}"
echo
echo "그 뒤:"
echo "  $D/run.sh preflight        # '기동 가능' 이 나와야 한다"
echo "  $REPO/tools/register-launchd.sh $NAME"
