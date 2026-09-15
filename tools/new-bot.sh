#!/bin/bash
# 봇 하나를 만드는 절차 중 스크립트로 되는 부분. 브라우저가 필요한 단계는
# 안내만 하고 멈춘다. 절차 전체는 docs/에이전트-추가-입력정보.md 에 있다.
#
#   tools/new-bot.sh <ascii이름> <표시이름> <엔진> <모델> [아이콘경로]
#
# 예: tools/new-bot.sh rei "아야나미 레이" gemini gemini-3.8-flash ~/Jobs/tasks/bot-assets/rei-icon.png
set -e
NAME=$1; DISPLAY=$2; ENGINE=$3; MODEL=$4; ICON=$5
REPO=/Users/taelkim/Jobs/slack-cli-agent
[ -z "$MODEL" ] && { sed -n '2,9p' "$0"; exit 2; }

case "$ENGINE" in
  claude) BIN=/Users/taelkim/.local/bin/claude ;;
  codex)  BIN=/Users/taelkim/.local/bin/codex ;;
  gemini) BIN=/opt/homebrew/bin/agy ;;
  *) echo "모르는 엔진: $ENGINE"; exit 2 ;;
esac

echo "== 1. 매니페스트"
# bot_user.display_name 은 ASCII 여야 한다. 한글은 앱 이름과 표시명에만 쓴다.
python3 - "$NAME" "$DISPLAY" > /tmp/$NAME-manifest.json <<'PY'
import json, sys
name, display = sys.argv[1], sys.argv[2]
print(json.dumps({
  "display_information": {"name": display, "description": f"{display} 에이전트", "background_color": "#1b2a3a"},
  "features": {
    "bot_user": {"display_name": name, "always_online": True},
    "assistant_view": {"assistant_description": "슬랙 안에서 조사와 작업을 맡는 에이전트입니다.", "suggested_prompts": []},
  },
  "oauth_config": {"scopes": {"bot": [
    "app_mentions:read","channels:history","groups:history","im:history","mpim:history","reactions:read",
    "channels:read","groups:read","im:read","mpim:read","users:read","users.profile:read","usergroups:read",
    "team:read","emoji:read","files:read","chat:write","chat:write.customize","chat:write.public",
    "reactions:write","files:write","im:write","channels:join","assistant:write"]}},
  "settings": {
    "event_subscriptions": {"bot_events": ["app_mention","message.channels","message.groups",
      "message.im","message.mpim","reaction_added","assistant_thread_started"]},
    "interactivity": {"is_enabled": False}, "org_deploy_enabled": False,
    "socket_mode_enabled": True, "token_rotation_enabled": False},
}, ensure_ascii=False))
PY

echo "== 2. 앱 생성"
APP_ID=$("$REPO/tools/slack-app.py" create tael /tmp/$NAME-manifest.json)
echo "  app_id=$APP_ID"

echo "== 3. 상태 디렉터리"
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

cat > "$D/run.sh" <<EOF
#!/bin/bash
# $DISPLAY 봇 기동. 토큰은 상태 디렉터리에서 읽는다
set -e
D="\$HOME/.$NAME"
export SLACK_BOT_TOKEN=\$(cat "\$D/.slack_bot_token")
export SLACK_APP_TOKEN=\$(cat "\$D/.slack_app_token")
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec "\$D/venv/bin/slack-cli-agent" "\$@" --profile $NAME --profile-dir $REPO/profiles
EOF
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

echo "== 4. 프로필"
python3 - "$NAME" "$DISPLAY" "$ENGINE" "$MODEL" "$BIN" "$REPO" <<'PY'
import json, sys
name, display, engine, model, binary, repo = sys.argv[1:7]
data = {
  "name": name, "display_name": display,
  "primary_engine": {"type": engine, "binary": binary, "model": model,
                     "model_owner": model, "home_dir": f"~/.{name}/engine/{engine}-home"},
  "state_dir": f"~/.{name}", "work_root": f"/Users/Shared/{name}-work",
  "launch_label": f"local.{name}", "owner_user_id": "U07CEJAV8",
  "troubleshoot_channel": "C0C1LNABECV", "plugins": [],
  "settings": {"request_timeout_sec": 900, "max_concurrent": 4,
               "base_tools": ["Read", "Grep", "Glob"],
               "owner_tools": ["Write", "Edit", "Bash", "NotebookEdit", "WebFetch", "WebSearch"]},
}
if engine == "codex":
    data["primary_engine"]["options"] = {"sandbox": "read-only", "network": False}
open(f"{repo}/profiles/{name}.json", "w", encoding="utf-8").write(
    json.dumps(data, ensure_ascii=False, indent=2) + "\n")
PY

echo "== 5. venv"
python3 -m venv "$D/venv"
"$D/venv/bin/pip" -q install -e "$REPO"

echo
echo "여기까지 자동이다. 남은 것은 브라우저로 한다."
echo "  1) 설치 승인   https://api.slack.com/apps/$APP_ID/install-on-team"
echo "     설치 후 OAuth 페이지에서 봇 토큰(xoxb)을 복사해"
echo "     $D/.slack_bot_token 에 넣고 chmod 600"
echo "  2) 앱 토큰     https://api.slack.com/apps/$APP_ID/general"
echo "     Generate Token and Scopes, 스코프는 connections:write"
echo "     $D/.slack_app_token 에 넣고 chmod 600"
[ -n "$ICON" ] && echo "  3) 아이콘      같은 /general 페이지의 앱 아이콘에 $ICON 을 올린다"
echo
echo "그 뒤:"
echo "  $D/run.sh preflight        # '기동 가능' 이 나와야 한다"
echo "  $REPO/tools/register-launchd.sh $NAME"
