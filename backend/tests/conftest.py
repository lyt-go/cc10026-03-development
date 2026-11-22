"""pytest 公共夹具：在导入应用模块前统一 DATA_ROOT，隔离各测试文件的文件产物。"""
from __future__ import annotations

import os
import shutil
import tempfile

TEMP_DATA = tempfile.mkdtemp(prefix="cc10026_test_data_")
os.environ["DATA_ROOT"] = TEMP_DATA


def _reset_state() -> None:
    from app.seed import SEED_ROWS
    from app.store import store
    from app.services import RECONCILIATION_SERVICE

    store._tables = {
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    }
    RECONCILIATION_SERVICE.reset()
    shutil.rmtree(TEMP_DATA, ignore_errors=True)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_root():
    """每个用例前后都重置内存仓库、复核状态与共享数据目录。"""
    _reset_state()
    yield
    # 用例结束后再次清空，避免落盘产物污染后续用例的目录断言
    shutil.rmtree(TEMP_DATA, ignore_errors=True)
