#!/usr/bin/env bash
# Bootstrap JobScout on a new machine (macOS or Linux).
#
#   git clone https://github.com/justinwan66/jobscout && cd jobscout && ./setup.sh
#
# Then copy your personal files from the old machine (they are NOT in git):
#   answers.json  profile.json  .imap_password   (and jobs.db to keep history)
set -euo pipefail
cd "$(dirname "$0")"

echo "== JobScout setup =="

command -v python3 >/dev/null || { echo "python3 required"; exit 1; }

# personal files: start from templates if absent
for f in profile answers; do
  if [ ! -f "$f.json" ]; then
    cp "$f.example.json" "$f.json"
    echo "created $f.json from template — FILL IT IN before applying to anything"
  fi
done

# venv + Playwright (only needed for apply.py / auto_apply.py / meta_board.py;
# the poller and dashboard are stdlib-only)
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip -q install playwright
  .venv/bin/python -m playwright install chromium
  echo "created .venv with Playwright + Chromium"
fi

mkdir -p logs

case "$(uname)" in
  Darwin)
    for plist in com.justinwan66.jobscout com.justinwan66.jobscout.dashboard; do
      sed "s|/Users/justinwan66/personal/jobscout|$(pwd)|g" "$plist.plist" \
        > ~/Library/LaunchAgents/"$plist.plist"
      launchctl bootout "gui/$(id -u)/$plist" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/"$plist.plist"
    done
    echo "launchd agents installed (poller 60s + dashboard)"
    ;;
  Linux)
    echo "add to crontab (crontab -e):"
    echo "  * * * * * cd $(pwd) && python3 scout.py run >> logs/cron.log 2>&1"
    echo "and run the dashboard under systemd/tmux: python3 dashboard.py"
    ;;
esac

echo
echo "next steps:"
echo "  1. fill in profile.json / answers.json (or copy them from your old machine)"
echo "  2. python3 scout.py run --seed     # first poll, no notification blast"
echo "  3. python3 scout.py test-notify    # verify notifications"
echo "  4. open http://localhost:8765"
echo "  note: run the POLLER on only one machine at a time (or notifications"
echo "        duplicate); the cloud workflow handles phone pushes regardless."
