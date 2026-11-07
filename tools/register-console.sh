#!/bin/bash
# 웹 콘솔을 launchd 에 등록해 상시 기동한다.
#
# 콘솔은 모든 봇을 함께 보여주므로 특정 봇에 매지 않는다. 전에는
# local.asuka.web 으로 등록돼 있어서, 아스카를 내리면 콘솔도 같이
# 내려갔다(2026-09-15 에 고침). 상태 디렉터리와 venv 도 봇과 따로 둔다.
#
# 마메치 대시보드(mametchi-slack-bot/dashboard)는 ~/.mametchi 만 읽어
# 구치파치 지표를 내지 못한다. 이 콘솔이 그 대체다(H8). 대시보드가
# 같은 8787 을 쓰므로 포트가 겹치면 등록하지 않고 멈춘다.
#
#   tools/register-console.sh [포트]
#
# 환경변수
#   CONSOLE_PROFILE_DIRS  프로필 디렉터리 목록, 콜론 구분. 기본 <리포>/profiles.
#                         봇 프로필(<이름>.json, example 제외)이 하나도 없는
#                         디렉터리는 거부한다. 작업 트리 사본의 profiles/ 에는
#                         example 만 있어 구치파치가 콘솔에 나오지 않는다
#   CONSOLE_HEALTH_TIMEOUT_SEC  기동 확인 대기 상한. 기본 20
#   LAUNCHCTL, LSOF, CURL  시험용 대역
set -e
PORT=${1:-8787}
REPO=$(cd "$(dirname "$0")/.." && pwd)
D="$HOME/.console"
LABEL=local.console.web
LAUNCHCTL=${LAUNCHCTL:-launchctl}
LSOF=${LSOF:-lsof}
CURL=${CURL:-curl}
HEALTH_TIMEOUT=${CONSOLE_HEALTH_TIMEOUT_SEC:-20}

fail() { echo "  $*" >&2; exit 1; }

# 프로필 디렉터리 확인. 봇 프로필이 없으면 콘솔이 빈 목록으로 뜬다.
IFS=: read -r -a PROFILE_DIRS <<< "${CONSOLE_PROFILE_DIRS:-$REPO/profiles}"
PROFILE_ARGS=""
for dir in "${PROFILE_DIRS[@]}"; do
  [ -d "$dir" ] || fail "프로필 디렉터리가 없다 : $dir"
  found=$(find "$dir" -maxdepth 1 -name '*.json' ! -name '*.example.json' | head -1)
  [ -n "$found" ] || fail "봇 프로필(<이름>.json)이 없는 디렉터리다 : $dir. 운영 작업 트리의 profiles/ 를 CONSOLE_PROFILE_DIRS 로 준다"
  PROFILE_ARGS="$PROFILE_ARGS --profile-dir \"$dir\""
done

mkdir -p "$D/logs" "$HOME/Library/LaunchAgents"

# venv 는 이 리포를 editable 로 설치한다. 다른 경로에서 설치된 venv 면
# 다시 설치한다. 그대로 두면 지워진 작업 트리의 코드를 계속 읽는다.
if [ ! -x "$D/venv/bin/slack-cli-agent" ] || [ "$(cat "$D/repo" 2>/dev/null)" != "$REPO" ]; then
  [ -x "$D/venv/bin/python" ] || python3 -m venv "$D/venv"
  "$D/venv/bin/pip" install -q -e "$REPO"
  echo "$REPO" > "$D/repo"
fi

cat > "$D/run.sh" <<EOF
#!/bin/bash
# 웹 콘솔 기동. 슬랙 토큰은 안 쓴다 - 파일만 읽고 쓴다.
set -e
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec "$D/venv/bin/slack-cli-agent" web$PROFILE_ARGS "\$@"
EOF
chmod +x "$D/run.sh"

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
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

# 이전 콘솔을 먼저 내린 뒤 포트를 본다. 그래도 점유 중이면 다른 프로세스다.
"$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
holder=$("$LSOF" -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$holder" ]; then
  fail "포트 $PORT 을 다른 프로세스(PID $(echo $holder | tr '\n' ' '))가 쓰고 있어 등록하지 않았다. 마메치 대시보드라면 내리거나 다른 포트를 인자로 준다"
fi

"$LAUNCHCTL" bootstrap "gui/$(id -u)" "$PLIST"

# launchctl list 에 이름이 있어도 bind 에 실패하면 재기동만 반복한다. HTTP 로 확인한다.
URL="http://127.0.0.1:$PORT"
waited=0
until "$CURL" -fsS -m 2 "$URL/api/health" 2>/dev/null | grep -q '"ok"'; do
  if [ "$waited" -ge "$HEALTH_TIMEOUT" ]; then
    fail "기동 확인 실패 : $URL/api/health 가 ${HEALTH_TIMEOUT}초 안에 응답하지 않았다. 로그 $D/logs/web.err.log"
  fi
  sleep 1
  waited=$((waited + 1))
done

echo "  $URL/"
echo "  봇 목록 : $("$CURL" -fsS -m 5 "$URL/api/bots" 2>/dev/null || echo '조회 실패')"
