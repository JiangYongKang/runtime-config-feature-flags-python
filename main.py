"""服务入口：启动时加载默认值 / 配置文件 / 环境变量三层配置。"""

from __future__ import annotations

import os

import uvicorn

from app.api import create_app
from app.manager import ConfigManager
from app.sources import build_config

config, _provenance = build_config(
    file_path=os.environ.get("FF_CONFIG_FILE"),
    environ=dict(os.environ),
)
manager = ConfigManager(config)
app = create_app(manager)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
