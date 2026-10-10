#!/usr/bin/env bash
# One-time setup of the DigitalOcean Droplet (Ubuntu 24.04). Run as root ON THE SERVER:
#   ssh whatsapp-bot 'bash -s' < deploy/server-setup.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -y
apt-get upgrade -y
apt-get install -y docker.io docker-compose-v2 ufw rsync unattended-upgrades
systemctl enable --now docker

# Firewall: SSH + HTTP(S) only.
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# 1 GB swap (the Droplet has 1 GB RAM; docker builds can spike).
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

mkdir -p /opt/whatsapp-bot
echo "Server ready."
