# Docker: альтернативный способ запуска

Основной путь описан в `START_IT.md`. Этот вариант предназначен для IT, если Docker принят в инфраструктуре. Docker-сборка в среде подготовки не запускалась: здесь нет Docker Engine. Перед вводом в эксплуатацию собрать и проверить её на целевом сервере.

1. Установить актуальный поддерживаемый Docker Engine с Compose v2. Не запускать параллельно `radar.service`.
2. Разместить проект в `/opt/radar`. По основной инструкции перенести `data/`, актуальный `config/` и `/etc/radar/radar.env`.
3. Приложение в контейнере работает с UID/GID **10001**, а не с пользователем `radar` хоста. Проверить, что этот ID допустим на сервере, и назначить права. Этот шаг заменяет права из инструкции systemd; не применять к работающей systemd-установке:

```bash
sudo chown -R 10001:10001 /opt/radar/data /opt/radar/config
sudo chmod -R u=rwX,g=,o= /opt/radar/data /opt/radar/config
sudo chown root:10001 /etc/radar/radar.env
sudo chmod 0640 /etc/radar/radar.env
cd /opt/radar/deploy
```

4. Задать `OLLAMA_IMAGE` с одобренной IT версией или digest образа, например через переменную окружения перед командами Compose. Реальное значение получить из используемого реестра; фиктивный digest в поставку не вписан. Зафиксировать значение в заявке и использовать его во всех последующих операциях Compose. Не хранить файл секретов приложения в корне проекта.
5. Проверить Compose и сначала запустить Ollama:

```bash
docker compose config --quiet
docker compose up -d ollama
docker compose exec ollama ollama pull hf.co/google/gemma-4-E4B-it-qat-q4_0-gguf
docker compose exec ollama ollama list
docker compose up -d --build radar
docker compose ps
docker compose logs --tail=80 radar
curl --fail --silent http://127.0.0.1:8080/health
```

Сборка использует `requirements.lock.txt`. Файл секретов подключается только при запуске в режиме чтения и исключён из контекста сборки. Данные — в `/opt/radar/data/`, настройки — в `/opt/radar/config/`. Порт 8080 привязан к loopback хоста, порт Ollama наружу не опубликован. Доступ к интерфейсу — через SSH по основной инструкции.

После переноса проверить эффективный адрес модели: `http://ollama:11434`. Настройка, сохранённая в SQLite, может переопределить переменную `LOCAL_LLM_URL`; исправить адрес через интерфейс. Имя модели должно совпадать с загруженным. Этот вариант по умолчанию использует CPU; доступ к GPU отдельно не настроен.

`unhealthy` в Docker — сигнал для мониторинга, сам по себе он не перезапускает контейнер. `restart: unless-stopped` перезапускает завершившийся процесс; работу приложения нужно контролировать отдельно.

Резервная копия: выполнить `docker compose stop radar`, затем под root с `umask 077` архивировать папки `data/` и `config/`, после чего выполнить `docker compose start radar`. Секреты хранить отдельно. Скрипт `deploy/backup.sh` предназначен для systemd и здесь не подходит. Не использовать `docker compose down -v` для обычного обслуживания: он удаляет именованный том Ollama.

Пройти весь список приёмки из `START_IT.md`, включая восстановление, доставку и известные расхождения тестов. Команды Compose требуют прав на Docker; доступ к Docker предоставлять только администраторам.
