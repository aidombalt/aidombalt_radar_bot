#!/usr/bin/env bash
# Радар · мост для Colab на VPS (Ubuntu 22.04/24.04, от root). Выполняется ОДИН раз.
# Готовую версию (с ключом ноутбука и адресом) Радар кладёт в data/vps_setup_ready.sh —
# откройте её в Блокноте, скопируйте ЦЕЛИКОМ и вставьте в консоль VPS (веб-консоль хостера или SSH).
#
# Что делает:
#   1) пользователь __USER__ без пароля и без оболочки: ключ ноутбука может ТОЛЬКО пробросить порт 127.0.0.1:__PORT__;
#   2) Caddy: https://__SITE__ с бесплатным сертификатом; наружу — только /api/brain/* и /health, остальное 403;
#   3) брандмауэр: 22, 80, 443.
set -euo pipefail
PUBKEY='__PUBKEY__'
SITE='__SITE__'
PORT='__PORT__'
RUSER='__USER__'

echo "== 1/3 пользователь для туннеля"
id "$RUSER" &>/dev/null || useradd -m -s /usr/sbin/nologin "$RUSER"
install -d -m 700 -o "$RUSER" -g "$RUSER" "/home/$RUSER/.ssh"
echo "restrict,port-forwarding,permitlisten=\"127.0.0.1:${PORT}\" ${PUBKEY}" > "/home/$RUSER/.ssh/authorized_keys"
chown "$RUSER:$RUSER" "/home/$RUSER/.ssh/authorized_keys"
chmod 600 "/home/$RUSER/.ssh/authorized_keys"
# на некоторых образах проброс портов в sshd выключен — разрешаем только этому пользователю и только «внутрь» VPS
cat > /etc/ssh/sshd_config.d/radar-bridge.conf <<CONF
Match User ${RUSER}
	AllowTcpForwarding remote
	X11Forwarding no
	PermitTTY no
CONF
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true

echo "== 2/3 Caddy (HTTPS)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
if ! apt-get install -y caddy; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi
cat > /etc/caddy/Caddyfile <<EOF
${SITE} {
	@bridge path /api/brain/* /health /api/max/webhook
	handle @bridge {
		reverse_proxy 127.0.0.1:${PORT} {
			transport http {
				read_timeout 120s
			}
		}
	}
	respond "Radar bridge" 403
}
EOF
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

echo "== 3/3 брандмауэр"
if command -v ufw >/dev/null; then
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null || true
fi

echo
echo "ГОТОВО. Адрес моста для Colab (секрет RADAR_URL):  https://${SITE}"
echo "Проверка: через 1–2 минуты после запуска Радара на ноутбуке  curl -s https://${SITE}/health  ответит {\"ok\": true, ...}"
