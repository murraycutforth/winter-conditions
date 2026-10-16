#!/usr/bin/env bash
# Install the hourly RuuviTag check (broadcast sample + history download).  Run: ./install-ruuvi.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

python3 -c "import bleak" 2>/dev/null || sudo apt-get install -y python3-bleak
# Pi OS ships with Bluetooth soft-blocked; systemd-rfkill keeps this setting across reboots
sudo rfkill unblock bluetooth

echo "Running the offline RuuviTag self-test..."
(cd "$DIR" && sudo -u "$USER_NAME" python3 -m tests.test_ruuvi_offline >/dev/null && echo "  OK")

for f in rain-archive-ruuvi.service rain-archive-ruuvi.timer; do
  sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" "$DIR/systemd/$f" | sudo tee "/etc/systemd/system/$f" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now rain-archive-ruuvi.timer
echo "Running the first check now (a full history download can take several minutes)..."
sudo systemctl start rain-archive-ruuvi.service || true
journalctl -u rain-archive-ruuvi -n 5 --no-pager
echo
echo "  python3 -m rainarchive.ruuvi -c config.toml status"
echo "  python3 -m rainarchive.ruuvi -c config.toml show --hours 24"
