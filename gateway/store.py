"""JSON 文件持久化 + 进程内 RLock。

- ``snapshot``/``restore`` 与领域组件之间通过 dict 交换数据；
- 写盘采用"临时文件 + os.replace"原子替换，避免重启读到半截文件；
- 锁对同一进程内的并发请求生效（FastAPI sync 路由 / 多线程 CLI 模拟）。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any


class JsonStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        if not self.path or not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        if not text:
            return {}  # 空文件（如 mkstemp 预建文件）视为无状态
        return json.loads(text)

    def save(self, data: dict[str, Any]) -> None:
        if not self.path:
            return  # 无路径 = 纯内存模式
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
