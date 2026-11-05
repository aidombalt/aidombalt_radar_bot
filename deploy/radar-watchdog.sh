#!/bin/sh
# Необязательный внешний watchdog для systemd; не используйте для Docker.
# Не отменяет остановку администратора и не перезапускает службу во время backup.sh.
systemctl is-active --quiet radar || exit 0
HB=/opt/radar/data/heartbeat
[ -f /opt/radar/data/stopped_by_user ] && exit 0
if ! curl -fsS -m 10 http://127.0.0.1:8080/health >/dev/null || [ -z "$(find "$HB" -mmin -10 2>/dev/null)" ]; then
  logger -t radar-watchdog 'Радар не отвечает — перезапуск'
  systemctl try-restart radar
fi
