"""群与番剧的绑定关系管理。

数据模型：
- main_group_id   : 总群 QQ 群号（来自 .env，不持久化在本文件）
- bindings        : alias → BindingEntry，**alias 全局唯一**（D9）
- group_index     : 群号 → [alias, ...]（derived；查询时实时算）

持久化：
- 文件：data/bindings.json，仅包含 bindings map（不含 main_group_id）
- 每次写操作（bind / unbind）后原子写盘（先写 .tmp 再 rename）

约束（D9）：
- 总群禁止 bind / unbind，调用 → MainGroupBindError
- alias 冲突 → AliasConflictError
- 一群多番时 resolve(hint=None) → AmbiguousShowError
- 该群无绑定 → NotBoundError
- alias 不存在 → AliasNotFoundError

注意 file_id / sheet_id 必须是 storage 接受的形态（腾讯：`300000000$xxx` 格式 fileID）。
命令层负责在调 bind() 之前做 encodedID → fileID 转换（见 storage.convert_encoded_id）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .exceptions import (
    AliasConflictError,
    AliasNotFoundError,
    AmbiguousShowError,
    MainGroupBindError,
    NotBoundError,
)
from .models import BindingEntry


class BindingStore:
    def __init__(
        self,
        path: Path,
        *,
        main_group_id: int | None = None,
    ) -> None:
        self._path = path
        self._main_group_id = main_group_id
        self._bindings: dict[str, BindingEntry] = {}

    # ================================================== load / save

    @classmethod
    def load(
        cls, path: Path, *, main_group_id: int | None = None
    ) -> "BindingStore":
        store = cls(path, main_group_id=main_group_id)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for alias, data in (raw.get("bindings") or {}).items():
                store._bindings[alias] = BindingEntry.from_dict(alias, data)
        return store

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bindings": {a: e.to_dict() for a, e in self._bindings.items()}
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    # ================================================== main group

    def is_main_group(self, group_id: int) -> bool:
        return self._main_group_id is not None and group_id == self._main_group_id

    @property
    def main_group_id(self) -> int | None:
        return self._main_group_id

    # ================================================== mutations

    def bind(
        self,
        *,
        group_id: int,
        alias: str,
        file_id: str,
        sheet_id: str,
        bound_by: int,
        bound_at: datetime | None = None,
    ) -> BindingEntry:
        if self.is_main_group(group_id):
            raise MainGroupBindError(
                f"群 {group_id} 是总群，禁止 /绑定 /解绑；请到对应工作群操作"
            )
        if alias in self._bindings:
            existing = self._bindings[alias]
            raise AliasConflictError(
                f"别名「{alias}」已被群 {existing.group_id} 绑定到 "
                f"{existing.file_id}/{existing.sheet_id}"
            )
        entry = BindingEntry(
            alias=alias,
            group_id=group_id,
            file_id=file_id,
            sheet_id=sheet_id,
            bound_by=bound_by,
            bound_at=bound_at or datetime.now(),
        )
        self._bindings[alias] = entry
        self.save()
        return entry

    def unbind(self, *, group_id: int, alias: str) -> BindingEntry:
        if self.is_main_group(group_id):
            raise MainGroupBindError(
                f"群 {group_id} 是总群，禁止 /解绑；请到对应工作群操作"
            )
        entry = self._bindings.get(alias)
        if entry is None:
            raise AliasNotFoundError(f"别名「{alias}」没有任何绑定")
        if entry.group_id != group_id:
            raise AliasNotFoundError(
                f"别名「{alias}」绑定在群 {entry.group_id}，不属于本群"
            )
        del self._bindings[alias]
        self.save()
        return entry

    # ================================================== read

    def get(self, alias: str) -> BindingEntry | None:
        return self._bindings.get(alias)

    def get_by_sheet(self, file_id: str, sheet_id: str) -> BindingEntry | None:
        """按 (file_id, sheet_id) 反查绑定（D17：把同步 diff 映射回工作群）。"""
        for e in self._bindings.values():
            if e.file_id == file_id and e.sheet_id == sheet_id:
                return e
        return None

    def get_for_group(self, group_id: int) -> list[BindingEntry]:
        return [e for e in self._bindings.values() if e.group_id == group_id]

    def list_all(self) -> list[BindingEntry]:
        return list(self._bindings.values())

    def aliases_for_group(self, group_id: int) -> list[str]:
        return [e.alias for e in self.get_for_group(group_id)]

    # ================================================== resolution (D9)

    def resolve(
        self,
        *,
        group_id: int,
        hint: str | None = None,
    ) -> BindingEntry:
        """根据群号 + 可选的番剧名 hint 解析到具体绑定。

        - hint 给了：必须能在绑定列表里找到，且 (a) 该绑定属于本群 OR (b) 调用方是总群
        - hint 缺省：
            - 总群：报错（总群不能省略 — 它没有"本群默认番剧"概念）
            - 工作群无绑定 → NotBoundError
            - 工作群单绑定 → 返回唯一一项
            - 工作群多绑定 → AmbiguousShowError(candidates)
        """
        if hint is not None:
            entry = self._bindings.get(hint)
            if entry is None:
                raise AliasNotFoundError(f"番剧「{hint}」未绑定到任何群")
            # 总群可查任何番剧；工作群只能查本群绑定的
            if not self.is_main_group(group_id) and entry.group_id != group_id:
                raise AliasNotFoundError(
                    f"番剧「{hint}」未绑定到本群（绑定在群 {entry.group_id}）"
                )
            return entry

        if self.is_main_group(group_id):
            raise AmbiguousShowError(
                group_id,
                [e.alias for e in self.list_all()],
            )

        own = self.get_for_group(group_id)
        if not own:
            raise NotBoundError(f"群 {group_id} 还没有绑定任何番剧")
        if len(own) == 1:
            return own[0]
        raise AmbiguousShowError(group_id, [e.alias for e in own])
