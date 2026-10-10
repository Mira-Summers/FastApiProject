"""入口模块。

PyCharm 的运行配置是 `Python.FastAPI` 类型，以 `main:app` 拉起 uvicorn，所以根目录
必须保留这个薄入口。真正的应用在 app/main.py。

不要在这里放业务逻辑，也不要有任何 import 期副作用。
"""

from __future__ import annotations

from app.main import app

__all__ = ["app"]
