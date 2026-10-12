#!/bin/zsh
# 배치 launchd 등록 (사용자 LaunchAgent — sudo 불필요). design/작업계획서.md §1.7
#
#   scripts/batch_launchd.sh install   <job>   plist 생성 · 등록 (00:10 daily + 상주 크롤러)
#   scripts/batch_launchd.sh status    <job>   등록 상태 + batch.py status
#   scripts/batch_launchd.sh uninstall <job>   등록 해제 (데이터는 그대로)
#   scripts/batch_launchd.sh print     <job>   plist 를 만들기만 하고 출력 (검증용)
#
# 환경변수 (선택):
#   SCHEDULE="HH:MM[,HH:MM...]"  daily 실행 시각 (기본 00:10). 예: SCHEDULE="08:05,08:14"
#   DAILY_ONLY=1                 크롤러 워커는 등록하지 않음 (시험용)
#   DAILY_ARGS="--max-rows 100"  daily 에 덧붙일 인자 (시험용)
#
# Mac 이 잠들면 작업이 멈춘다. 전원 연결 · 잠자기 방지를 켜 둘 것.
# 00:05 자동 깨우기(권장): sudo pmset repeat wakeorpoweron MTWRFSU 00:05:00
set -euo pipefail

cmd=${1:-}; job=${2:-}
[[ -z "$cmd" || -z "$job" ]] && { sed -n '2,10p' "$0"; exit 2; }

root=${0:A:h:h}
py="$root/.venv/bin/python"
agents="$HOME/Library/LaunchAgents"
logdir="$root/logs/batch"
label_daily="kr.lguplus.bizcrawler.$job.daily"
label_crawl="kr.lguplus.bizcrawler.$job.crawl"
domain="gui/$(id -u)"

plist() {   # $1 label · $2 KeepAlive(true/false) · $3.. 인자
  local label=$1 keep=$2; shift 2
  local args="" a
  for a in /usr/bin/caffeinate -i "$py" "$root/scripts/batch.py" "$@"; do
    args+="    <string>${a}</string>\n"
  done
  local sched="" t h m
  if [[ $keep == false ]]; then
    sched="  <key>StartCalendarInterval</key>\n  <array>\n"
    for t in ${(s:,:)${SCHEDULE:-00:10}}; do
      h=${t%%:*}; m=${t##*:}
      sched+="    <dict><key>Hour</key><integer>$((10#$h))</integer><key>Minute</key><integer>$((10#$m))</integer></dict>\n"
    done
    sched+="  </array>\n"
  else
    sched="  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n"
  fi
  printf '%b' "<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key>
  <array>
${args}  </array>
  <key>WorkingDirectory</key><string>${root}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SSL_CERT_FILE</key><string>${root}/certs/ca-bundle.pem</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
${sched}  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>${logdir}/${label}.out.log</string>
  <key>StandardErrorPath</key><string>${logdir}/${label}.err.log</string>
</dict>
</plist>
"
}

case $cmd in
  print)
    plist "$label_daily" false daily --job "$job" --notify
    plist "$label_crawl" true crawl-worker --job "$job"
    ;;
  install)
    [[ -x "$py" ]] || { echo "venv python 없음: $py"; exit 1; }
    mkdir -p "$agents" "$logdir"
    plist "$label_daily" false daily --job "$job" --notify ${(z)DAILY_ARGS:-} > "$agents/$label_daily.plist"
    labels=("$label_daily")
    if [[ -z "${DAILY_ONLY:-}" ]]; then
      plist "$label_crawl" true crawl-worker --job "$job" > "$agents/$label_crawl.plist"
      labels+=("$label_crawl")
    fi
    for l in $labels; do
      plutil -lint "$agents/$l.plist" >/dev/null
      launchctl bootout "$domain/$l" 2>/dev/null || true
      launchctl bootstrap "$domain" "$agents/$l.plist"
      echo "등록: $l"
    done
    echo "로그: $logdir"
    ;;
  status)
    for l in "$label_daily" "$label_crawl"; do
      if launchctl print "$domain/$l" >/dev/null 2>&1; then
        echo "$l: 등록됨 ($(launchctl print "$domain/$l" | awk -F'= ' '/state =/{print $2; exit}'))"
      else
        echo "$l: 미등록"
      fi
    done
    "$py" "$root/scripts/batch.py" status --job "$job"
    ;;
  uninstall)
    for l in "$label_daily" "$label_crawl"; do
      launchctl bootout "$domain/$l" 2>/dev/null && echo "해제: $l" || echo "미등록: $l"
      rm -f "$agents/$l.plist"
    done
    ;;
  *) echo "알 수 없는 명령: $cmd"; exit 2 ;;
esac
