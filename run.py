"""Запуск Радара:  python run.py            (GUI на http://сервер:8080)
                  python run.py --once hot  (один цикл сбора — проверка настроек)
                  python run.py --report 24 (отчёт за сутки в data/reports)"""
from monitor.app import main

if __name__ in {"__main__", "__mp_main__"}:
    main()
