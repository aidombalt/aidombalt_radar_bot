#!/usr/bin/env bash
# Запуск от root: sudo bash /opt/radar/deploy/backup.sh
# Согласованная копия data/ и config/ с короткой остановкой службы.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Запустите через sudo.' >&2; exit 1; }
umask 077
install -d -m 700 /var/backups/radar
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dest="/var/backups/radar/radar-${stamp}-$$.tar.gz"
was_active=0
systemctl is-active --quiet radar && was_active=1
resume() { if [ "$was_active" = 1 ]; then systemctl start radar; fi; }
trap resume EXIT
systemctl stop radar
tar -czf "${dest}.partial" -C /opt/radar data config HANDOVER_VERSION
mv "${dest}.partial" "$dest"
sha256sum "$dest" > "${dest}.sha256"
echo "Копия сохранена: $dest"
echo 'Секреты /etc/radar/radar.env в неё не включены; храните их отдельно.'
