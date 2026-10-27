#!/usr/bin/env bash
# One-time setup on Ubuntu (22.04 or 24.04). Run from the project folder:  bash scripts/install_ubuntu.sh
set -euo pipefail
sudo apt-get update
sudo apt-get install -y mininet openvswitch-switch iproute2 iputils-ping ethtool iperf git python3-pip python3-venv
sudo systemctl enable --now openvswitch-switch 2>/dev/null || sudo service openvswitch-switch start

# virtual environment that can also see the system-wide mininet module
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install matplotlib
bash scripts/install_ryu.sh

echo
echo "Setup finished. Check the network layer with:   sudo mn --test pingall"
echo "Activate the environment in every new terminal: source .venv/bin/activate"
