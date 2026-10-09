#!/usr/bin/env bash
# Install the read-only crag conditions dashboard as a service on port 8080.  Run: ./install-dashboard.sh
# LAN/Tailscale only: it has no authentication, so never port-forward it.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

echo "Running the offline dashboard self-test..."
# as the owning user, so a sudo run doesn't leave root-owned __pycache__ files in the repo
(cd "$DIR" && sudo -u "$USER_NAME" python3 -m tests.test_dashboard_offline >/dev/null && echo "  OK")

f=rain-archive-dashboard.service
sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" "$DIR/systemd/$f" | sudo tee "/etc/systemd/system/$f" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now "$f"
sudo systemctl restart "$f"    # pick up new code on re-runs
sleep 2
systemctl is-active --quiet "$f" && echo "Dashboard running: http://$(hostname).local:8080/" \
  || { echo "Dashboard failed to start:"; journalctl -u "$f" -n 20 --no-pager; exit 1; }
