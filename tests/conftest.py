"""测试公共夹具与日志配置。

需求：单测日志里打印输入和判定依据。这里把 feature_flags logger 调到
DEBUG 并输出到控制台，caplog 也会捕获同样的记录。
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

from app.flags.manager import FlagManager

# 让 [flag-eval] / [flag-cache-hit] 判定日志在测试输出中可见
_logger = logging.getLogger("feature_flags")
_logger.setLevel(logging.DEBUG)
if not any(
    isinstance(h, logging.StreamHandler) and getattr(h, "_flag_test_handler", False)
    for h in _logger.handlers
):
    _h = logging.StreamHandler(sys.stdout)
    _h.setLevel(logging.DEBUG)
    _h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )
    _h._flag_test_handler = True  # type: ignore[attr-defined]
    _logger.addHandler(_h)
_logger.propagate = True


@pytest.fixture
def manager() -> FlagManager:
    """不读文件、不读真实环境变量的干净管理器（仅内置默认值）。"""
    return FlagManager(config_path=None, environ={})


@pytest.fixture
def tmp_config_file(tmp_path):
    """返回一个向临时 JSON 配置文件写入内容的辅助函数。"""

    def _write(payload: str) -> str:
        path = tmp_path / "flags.json"
        path.write_text(payload, encoding="utf-8")
        return str(path)

    return _write


# 保证测试不受宿主环境污染
os.environ.pop("FEATURE_FLAGS_CONFIG_FILE", None)
