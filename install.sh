#!/usr/bin/env bash
# Install rain-archive as an always-on systemd service on Raspberry Pi OS (Bookworm or later).
# Run from inside the rain-archive folder:  ./install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

echo "Installing Python dependencies from apt..."
sudo apt-get update -qq
sudo apt-get install -y python3-numpy python3-pil python3-requests sqlite3

if [ ! -f "$DIR/config.toml" ]; then
  cp "$DIR/config.example.toml" "$DIR/config.toml"
  echo "Created config.toml (edit data_dir if you want the archive on a USB drive)."
fi

echo "Running the offline self-test..."
(cd "$DIR" && python3 -m tests.test_offline)

echo "Doing one live poll to check RainViewer is reachable..."
(cd "$DIR" && python3 -m rainarchive -c "$DIR/config.toml" once)

sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" "$DIR/systemd/rain-archive.service" \
  | sudo tee /etc/systemd/system/rain-archive.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now rain-archive.service

echo
echo "Done. Useful commands:"
echo "  systemctl status rain-archive        # is it running?"
echo "  journalctl -u rain-archive -f        # live log"
echo "  python3 -m rainarchive -c config.toml status"
echo "  python3 -m rainarchive -c config.toml render --latest -o latest.png"
