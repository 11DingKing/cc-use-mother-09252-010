"""可替换端口：时钟、标识生成、快照存储。"""
from __future__ import annotations

import abc
from typing import Any


class Clock(abc.ABC):
    @abc.abstractmethod
    def now(self) -> str:
        """返回 ISO 时间字符串。"""


class IdGenerator(abc.ABC):
    @abc.abstractmethod
    def next_id(self, kind: str) -> str:
        """生成某类实体的全局唯一标识。"""


class SnapshotStore(abc.ABC):
    @abc.abstractmethod
    def load(self) -> dict[str, Any] | None:
        """读取最近一次快照；无快照返回 None。"""

    @abc.abstractmethod
    def save(self, snapshot: dict[str, Any]) -> None:
        """原子写入快照。"""
