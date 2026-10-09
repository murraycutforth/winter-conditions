#!/usr/bin/env bash
# Add the crag weather extension to an existing rain-archive install.  Run: ./install-weather.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

if ! grep -q '^\[\[crags\]\]' "$DIR/config.toml" 2>/dev/null; then
  echo "config.toml has no [[crags]] section: the 12 built-in crags will be used."
  echo "(Copy the [weather] and [[crags]] blocks from config.example.toml to customise them.)"
fi

echo "Running the offline weather self-test..."
(cd "$DIR" && python3 -m tests.test_weather_offline >/dev/null && echo "  OK")

for f in rain-archive-weather.service rain-archive-weather.timer; do
  sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" "$DIR/systemd/$f" | sudo tee "/etc/systemd/system/$f" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now rain-archive-weather.timer
echo "Starting the first update now (backfill continues over the next ~2 days within Open-Meteo's free limits)..."
sudo systemctl start --no-block rain-archive-weather.service
echo
echo "  journalctl -u rain-archive-weather -f                       # watch progress"
echo "  python3 -m rainarchive.weather -c config.toml status         # backfill progress"
echo "  python3 -m rainarchive.weather -c config.toml show 'Ben Nevis'"
