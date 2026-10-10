#!/usr/bin/env bash
# One-time (idempotent) CI/CD prep of the Droplet. Run as root ON THE SERVER, from your Mac:
#   ssh whatsapp-bot "bash -s -- '$(cat ~/.ssh/whatsapp_bot_ci.pub)'" < deploy/setup-ci-user.sh
# - creates user 'deploy' (no password, bash, in group docker) for GitHub Actions
# - installs the CI public key for it (no forwarding, no pty)
# - gives /opt/whatsapp-bot to 'deploy' (.env stays 600)
# - sshd: password login off, root key login kept (validated with sshd -t before reload)
set -euo pipefail
DEST=/opt/whatsapp-bot
PUBKEY="${1:-}"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -n "$PUBKEY" ]] || { echo "usage: setup-ci-user.sh '<ssh-ed25519 AAAA... github-actions-deploy>'" >&2; exit 1; }
ssh-keygen -l -f /dev/stdin <<<"$PUBKEY" >/dev/null 2>&1 || { echo "not a valid public key" >&2; exit 1; }
command -v rsync >/dev/null || apt-get install -y rsync

# 1. User 'deploy': no password (locked), bash, can run docker.
#    Note: group docker is root-equivalent on this host; the key below is the only way in.
if ! id deploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash deploy
  echo "created user deploy"
fi
usermod -aG docker deploy

# 2. CI key -> ~deploy/.ssh/authorized_keys (added once).
HOME_DIR="$(getent passwd deploy | cut -d: -f6)"
install -d -m 700 -o deploy -g deploy "$HOME_DIR/.ssh"
AK="$HOME_DIR/.ssh/authorized_keys"
touch "$AK"
KEY_BODY="$(awk '{print $1" "$2}' <<<"$PUBKEY")"
if ! grep -qF "$KEY_BODY" "$AK"; then
  echo "no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty $PUBKEY" >> "$AK"
  echo "installed CI key"
fi
chown deploy:deploy "$AK" && chmod 600 "$AK"

# 3. App dir belongs to deploy; secrets stay private.
mkdir -p "$DEST"
chown -R deploy:deploy "$DEST"
for f in "$DEST"/.env "$DEST"/.env.bak*; do
  if [[ -e "$f" ]]; then chmod 600 "$f"; fi
done

# 4. sshd: no passwords, root by key only. Only if root really has a key, so you're not locked out.
if [[ ! -s /root/.ssh/authorized_keys ]]; then
  echo "WARNING: /root/.ssh/authorized_keys is empty; NOT changing sshd" >&2
else
  # 00- so it wins over cloud-init's 50-cloud-init.conf (first value wins in sshd_config).
  CONF=/etc/ssh/sshd_config.d/00-whatsapp-bot-hardening.conf
  cat > "$CONF.new" <<'EOF'
# Managed by deploy/setup-ci-user.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
EOF
  if cmp -s "$CONF.new" "$CONF"; then
    rm "$CONF.new"
  else
    [[ -f "$CONF" ]] && cp "$CONF" "$CONF.bak"
    mv "$CONF.new" "$CONF"
    if sshd -t; then
      systemctl reload ssh 2>/dev/null || systemctl reload sshd
      echo "sshd hardened and reloaded"
    else
      echo "sshd -t failed; reverting" >&2
      if [[ -f "$CONF.bak" ]]; then mv "$CONF.bak" "$CONF"; else rm -f "$CONF"; fi
      exit 1
    fi
  fi
fi

echo "--- check"
sshd -T 2>/dev/null | grep -Ei '^(passwordauthentication|permitrootlogin|pubkeyauthentication) '
runuser -u deploy -- docker ps --format '{{.Names}}: {{.Status}}'
echo "OK. Now test from your Mac:"
echo "  ssh whatsapp-bot true    # root still works"
echo "  ssh -i ~/.ssh/whatsapp_bot_ci deploy@<droplet> 'docker ps'   # CI user works"
