import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RADAR_DATA_DIR", tempfile.mkdtemp(prefix="radar_test_"))

import pytest  # noqa: E402

from monitor.context import Ctx  # noqa: E402
from monitor.db import DB  # noqa: E402
from monitor.settings import load_settings  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture()
def ctx(tmp_path):
    s = load_settings({"brain": {"local": {"enabled": False}}, "report_emails": ["pr@example.ru"]})
    c = Ctx(settings=s, db=DB(tmp_path / "t.sqlite3"))
    c.sync_sources_from_yaml()
    c.db.x("INSERT INTO users(tg_id, name, role, level, active) VALUES('111','Оператор','admin',2,1)")
    return c
