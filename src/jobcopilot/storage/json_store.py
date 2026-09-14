"""极简 JSON 文件存储。

内核本身不落盘，但**默认实现**放在这里供宿主直接复用（SEKB 用它写投递计划、
CLI 用它写本地配置）。宿主也可以完全不用它，自己实现
:class:`~jobcopilot.core.ports.KVStorePort`。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: 数据目录环境变量；默认 ``data``（与 SEKB 现有相对路径保持一致）
ENV_DATA_DIR = "JOBCOPILOT_DATA_DIR"


def data_dir() -> Path:
    """返回数据目录（受 ``JOBCOPILOT_DATA_DIR`` 控制，默认 ``data``）。"""
    return Path(os.environ.get(ENV_DATA_DIR, "data"))


class JsonFileStore:
    """把一个 JSON 对象整体读写到单个文件。

    读失败（文件不存在 / 内容损坏 / 无权限）一律返回空字典，**不抛异常**——
    分析链路的可用性优先于「让调用方知道存储坏了」，异常由调用方按需自行校验。

    Args:
        path: 文件路径；父目录不存在时写入前自动创建。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """存储文件路径。"""
        return self._path

    def load(self) -> dict[str, Any]:
        """读出全部数据；失败返回 ``{}``。"""
        if not self._path.exists():
            return {}
        try:
            out: dict[str, Any] = json.loads(self._path.read_text(encoding="utf-8"))
            return out
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        """整体写回（``ensure_ascii=False`` + 两空格缩进，便于人工查看）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
