"""pytest 公共夹具与日志配置。

需求要求“单测日志里打印输入和判定依据”：这里把 feature_flags logger
直接输出到 stdout（pytest 以 -s 运行即可见），并提供一个记录辅助函数，
各用例在断言前统一打印输入参数、期望与实际判定依据。
"""

from __future__ import annotations

import logging
import sys

import pytest

from app.api import create_app
from app.manager import ConfigManager
from app.models import FeatureFlagsConfig
from app.sources import build_config

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
LOGGER = logging.getLogger("feature_flags.tests")


def log_case(title: str, **inputs) -> None:
    """打印用例输入，随后由调用方打印判定依据。"""
    rendered = ", ".join(f"{k}={v!r}" for k, v in inputs.items())
    LOGGER.info("用例[%s] 输入: %s", title, rendered)


def log_evaluation(title: str, evaluation) -> None:
    LOGGER.info("用例[%s] 结论: on=%s matched=%s", title, evaluation.on, evaluation.matched)
    for step in evaluation.basis:
        LOGGER.info("用例[%s] 依据: %s", title, step)


@pytest.fixture
def sample_config() -> FeatureFlagsConfig:
    return FeatureFlagsConfig.from_dict(
        {
            "new_checkout": {
                "enabled": True,
                "allowed_callers": ["alice"],
                "denied_callers": ["mallory"],
                "rollout_percentage": 50,
            }
        }
    )


@pytest.fixture
def manager(sample_config) -> ConfigManager:
    return ConfigManager(sample_config)


@pytest.fixture
def client(manager):
    from fastapi.testclient import TestClient

    app = create_app(manager)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def example_file(tmp_path):
    path = tmp_path / "flags.json"
    path.write_text(
        '{"flags": {"new_checkout": {"enabled": true, '
        '"rollout_percentage": 25, "allowed_callers": ["alice", "bob"], '
        '"denied_callers": []}}}',
        encoding="utf-8",
    )
    return str(path)
