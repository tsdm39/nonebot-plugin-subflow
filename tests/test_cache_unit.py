"""cache.py 单元测试 — 用 FakeStorage 不打网络。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nonebot_plugin_subflow.cache import SheetCache, SheetDiff
from nonebot_plugin_subflow.exceptions import RecordNotFoundError
from nonebot_plugin_subflow.models import FieldSchema, Record
from nonebot_plugin_subflow.storage.base import StorageBackend


# =================================================================== fake storage


class FakeStorage(StorageBackend):
    """In-memory storage backend for tests. Records call activity for assertions."""

    def __init__(self) -> None:
        self.fields: dict[tuple[str, str], list[FieldSchema]] = {}
        self.records: dict[tuple[str, str], dict[str, Record]] = {}
        self.calls: list[tuple[str, tuple]] = []
        self._auto_id = 0

    def configure(
        self,
        file_id: str,
        sheet_id: str,
        *,
        fields: list[FieldSchema] | None = None,
        records: list[Record] | None = None,
    ) -> None:
        self.fields[(file_id, sheet_id)] = fields or []
        self.records[(file_id, sheet_id)] = {r.record_id: r for r in (records or [])}

    async def get_fields(
        self, file_id: str, sheet_id: str, *, force_refresh: bool = False
    ) -> list[FieldSchema]:
        self.calls.append(("get_fields", (file_id, sheet_id)))
        return self.fields.get((file_id, sheet_id), [])

    async def get_records(self, file_id: str, sheet_id: str) -> list[Record]:
        self.calls.append(("get_records", (file_id, sheet_id)))
        return list(self.records.get((file_id, sheet_id), {}).values())

    async def get_record(
        self, file_id: str, sheet_id: str, record_id: str
    ) -> Record | None:
        self.calls.append(("get_record", (file_id, sheet_id, record_id)))
        existing = self.records.get((file_id, sheet_id), {}).get(record_id)
        if existing is None:
            return None
        # 模拟真实 storage 每次都返回新 Record，避免上层意外共享可变状态
        return Record(record_id=existing.record_id, values=dict(existing.values))

    async def add_records(
        self, file_id: str, sheet_id: str, rows: list[dict[str, Any]]
    ) -> list[Record]:
        self.calls.append(("add_records", (file_id, sheet_id, len(rows))))
        sheet = self.records.setdefault((file_id, sheet_id), {})
        out: list[Record] = []
        for row in rows:
            self._auto_id += 1
            rid = f"fake{self._auto_id}"
            rec = Record(record_id=rid, values=dict(row))
            sheet[rid] = rec
            out.append(rec)
        return out

    async def update_record(
        self,
        file_id: str,
        sheet_id: str,
        record_id: str,
        values: dict[str, Any],
    ) -> Record:
        self.calls.append(("update_record", (file_id, sheet_id, record_id)))
        sheet = self.records.setdefault((file_id, sheet_id), {})
        existing = sheet.get(record_id)
        if existing is None:
            raise RecordNotFoundError(record_id)
        # 模拟真实 storage 的语义：不原地改老 Record，而是替换成新对象
        merged = dict(existing.values)
        merged.update(values)
        sheet[record_id] = Record(record_id=record_id, values=merged)
        # mimic sparse response (only updated fields)
        return Record(record_id=record_id, values=dict(values))

    async def delete_records(
        self, file_id: str, sheet_id: str, record_ids: list[str]
    ) -> None:
        self.calls.append(("delete_records", (file_id, sheet_id, tuple(record_ids))))
        sheet = self.records.setdefault((file_id, sheet_id), {})
        for rid in record_ids:
            sheet.pop(rid, None)


# =================================================================== fixtures


F = ("file_X", "sheet_Y")


@pytest.fixture
def fake() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def cache(fake: FakeStorage) -> SheetCache:
    return SheetCache(fake, sync_interval_minutes=99999)  # never auto-sync in tests


# =================================================================== sheet registration


async def test_add_sheet_loads_records(fake: FakeStorage, cache: SheetCache) -> None:
    fake.configure(*F, records=[Record("r1", {"备注": "x"}), Record("r2", {"备注": "y"})])
    count = await cache.add_sheet(*F)
    assert count == 2
    assert cache.list_sheets() == [F]
    assert {r.record_id for r in cache.get_records(*F)} == {"r1", "r2"}


async def test_remove_sheet_clears_records_and_locks(
    fake: FakeStorage, cache: SheetCache
) -> None:
    fake.configure(*F, records=[Record("r1", {})])
    await cache.add_sheet(*F)
    cache.lock_for(*F, "r1")  # create a lock
    cache.remove_sheet(*F)
    assert cache.list_sheets() == []
    assert cache.get_records(*F) == []
    # lock dict should be empty for this sheet
    assert all(k[0:2] != F for k in cache._locks)  # type: ignore[attr-defined]


# =================================================================== read API zero-cost


async def test_get_record_does_not_hit_storage(
    fake: FakeStorage, cache: SheetCache
) -> None:
    fake.configure(*F, records=[Record("r1", {"备注": "x"})])
    await cache.add_sheet(*F)
    fake.calls.clear()
    rec = cache.get_record(*F, "r1")
    assert rec is not None and rec.values["备注"] == "x"
    assert fake.calls == [], "查询不应该打 storage"


async def test_find_records_filters(fake: FakeStorage, cache: SheetCache) -> None:
    fake.configure(
        *F,
        records=[
            Record("r1", {"进度": "未分配"}),
            Record("r2", {"进度": "已完成"}),
            Record("r3", {"进度": "未分配"}),
        ],
    )
    await cache.add_sheet(*F)
    hits = cache.find_records(*F, lambda r: r.values["进度"] == "未分配")
    assert {r.record_id for r in hits} == {"r1", "r3"}


# =================================================================== locks


def test_lock_for_returns_same_object_for_same_key(cache: SheetCache) -> None:
    l1 = cache.lock_for(*F, "r1")
    l2 = cache.lock_for(*F, "r1")
    l3 = cache.lock_for(*F, "r2")
    assert l1 is l2
    assert l1 is not l3


async def test_concurrent_updates_serialize_under_lock(
    fake: FakeStorage, cache: SheetCache
) -> None:
    """D5: 两个并发 update 在 lock 下应该串行；不会出现交叉写后 race。"""
    fake.configure(*F, records=[Record("r1", {"组员": ""})])
    await cache.add_sheet(*F)

    results: list[str] = []

    async def claim(claimant: str) -> None:
        async with cache.lock_for(*F, "r1"):
            current = cache.get_record(*F, "r1")
            assert current is not None
            if current.values.get("组员"):
                results.append(f"{claimant}-denied")
                return
            await cache.update_record(*F, "r1", {"组员": claimant})
            results.append(f"{claimant}-claimed")

    await asyncio.gather(claim("小明"), claim("小红"))
    # 不管谁先抢到，结果集合一定是 1 个 -claimed + 1 个 -denied
    suffixes = sorted(r.split("-", 1)[1] for r in results)
    assert suffixes == ["claimed", "denied"]
    # 最终 storage 里的组员就是先抢到的那个
    claimant = next(r.split("-", 1)[0] for r in results if r.endswith("claimed"))
    assert fake.records[F]["r1"].values["组员"] == claimant


# =================================================================== writes


async def test_add_records_populates_cache_and_calls_storage(
    fake: FakeStorage, cache: SheetCache
) -> None:
    inserted = await cache.add_records(*F, [{"备注": "a"}, {"备注": "b"}])
    assert len(inserted) == 2
    assert len(cache.get_records(*F)) == 2
    # storage saw add_records once with 2 rows
    add_calls = [c for c in fake.calls if c[0] == "add_records"]
    assert add_calls == [("add_records", (*F, 2))]


async def test_update_record_does_write_then_reread(
    fake: FakeStorage, cache: SheetCache
) -> None:
    """D8: cache.update_record 应在 storage.update_record 之后 immediately call storage.get_record。"""
    fake.configure(*F, records=[Record("r1", {"备注": "old"})])
    await cache.add_sheet(*F)
    fake.calls.clear()
    rec = await cache.update_record(*F, "r1", {"备注": "new"})
    assert rec.values["备注"] == "new"
    # 顺序：update_record then get_record
    types = [c[0] for c in fake.calls]
    assert types == ["update_record", "get_record"], f"got {types}"


async def test_update_record_raises_when_record_vanishes(
    fake: FakeStorage, cache: SheetCache
) -> None:
    """update 调用后远端记录被别处删了（罕见但可能）→ cache 应丢掉并报 RecordNotFoundError。"""
    fake.configure(*F, records=[Record("r1", {"备注": "x"})])
    await cache.add_sheet(*F)

    # monkey-patch storage to delete the record after update
    original_update = fake.update_record

    async def vanishing_update(
        file_id: str, sheet_id: str, record_id: str, values: dict[str, Any]
    ) -> Record:
        result = await original_update(file_id, sheet_id, record_id, values)
        fake.records[(file_id, sheet_id)].pop(record_id, None)
        return result

    fake.update_record = vanishing_update  # type: ignore[method-assign]

    with pytest.raises(RecordNotFoundError):
        await cache.update_record(*F, "r1", {"备注": "y"})
    assert cache.get_record(*F, "r1") is None


async def test_delete_records_removes_from_cache_and_storage(
    fake: FakeStorage, cache: SheetCache
) -> None:
    fake.configure(*F, records=[Record("r1", {}), Record("r2", {})])
    await cache.add_sheet(*F)
    cache.lock_for(*F, "r1")  # create lock
    await cache.delete_records(*F, ["r1"])
    assert cache.get_record(*F, "r1") is None
    assert cache.get_record(*F, "r2") is not None
    # lock should be cleared
    assert (*F, "r1") not in cache._locks  # type: ignore[attr-defined]


# =================================================================== refresh


async def test_refresh_record_updates_from_storage(
    fake: FakeStorage, cache: SheetCache
) -> None:
    fake.configure(*F, records=[Record("r1", {"备注": "old"})])
    await cache.add_sheet(*F)
    # mutate storage out-of-band
    fake.records[F]["r1"].values["备注"] = "remote-edit"
    refreshed = await cache.refresh_record(*F, "r1")
    assert refreshed is not None
    assert refreshed.values["备注"] == "remote-edit"
    # cache reflects too
    assert cache.get_record(*F, "r1").values["备注"] == "remote-edit"  # type: ignore[union-attr]


async def test_refresh_record_removes_when_storage_returns_none(
    fake: FakeStorage, cache: SheetCache
) -> None:
    fake.configure(*F, records=[Record("r1", {})])
    await cache.add_sheet(*F)
    fake.records[F].pop("r1")  # vanish out-of-band
    result = await cache.refresh_record(*F, "r1")
    assert result is None
    assert cache.get_record(*F, "r1") is None


async def test_refresh_all_iterates_all_sheets(
    fake: FakeStorage, cache: SheetCache
) -> None:
    F2 = ("file_X", "sheet_Z")
    fake.configure(*F, records=[Record("a", {})])
    fake.configure(*F2, records=[Record("b", {}), Record("c", {})])
    await cache.add_sheet(*F)
    await cache.add_sheet(*F2)
    fake.calls.clear()
    results = await cache.refresh_all()
    # 数据未变 → 两表都是空 diff
    assert set(results.keys()) == {F, F2}
    assert all(
        isinstance(d, SheetDiff) and d.is_empty() for d in results.values()
    )
    assert cache.last_sync_at is not None


async def test_refresh_all_continues_on_single_sheet_failure(
    fake: FakeStorage, cache: SheetCache
) -> None:
    F2 = ("broken", "broken")
    fake.configure(*F, records=[Record("r1", {})])
    await cache.add_sheet(*F)
    cache._sheets[F2] = {}  # type: ignore[attr-defined]
    # leave F2 not configured in storage → get_records returns []
    results = await cache.refresh_all()
    assert isinstance(results[F], SheetDiff)


# =================================================================== D17 diff

V_UNASSIGNED = {"进度": "未分配", "组员": ""}


async def test_refresh_sheet_diff_first_load_is_empty(
    fake: FakeStorage, cache: SheetCache
) -> None:
    """子表此前不在缓存（首次加载）→ 空 diff，不把存量行当变更。"""
    fake.configure(*F, records=[Record("a", dict(V_UNASSIGNED))])
    diff = await cache.refresh_sheet_diff(*F)
    assert diff.is_empty()


async def test_refresh_sheet_diff_detects_added_changed_removed(
    fake: FakeStorage, cache: SheetCache
) -> None:
    fake.configure(
        *F,
        records=[
            Record("a", {"进度": "未分配", "组员": ""}),
            Record("b", {"进度": "已分配", "组员": "100"}),
        ],
    )
    await cache.add_sheet(*F)
    # 外部直改：a 被接走、b 整行删除、新增 c
    fake.records[F] = {
        "a": Record("a", {"进度": "已分配", "组员": "200"}),
        "c": Record("c", {"进度": "未分配", "组员": ""}),
    }
    diff = await cache.refresh_sheet_diff(*F)
    assert [r.record_id for r in diff.added] == ["c"]
    assert [r.record_id for r in diff.removed] == ["b"]
    assert len(diff.changed) == 1
    old, new = diff.changed[0]
    assert old.record_id == "a" and old.values["组员"] == ""
    assert new.values["组员"] == "200"
    # 覆盖后缓存为新值
    assert cache.get_record(*F, "a").values["组员"] == "200"


async def test_sync_loop_invokes_on_sync_changes(fake: FakeStorage) -> None:
    """_sync_loop 跑完一轮把非空 diff 交给回调。"""
    cache = SheetCache(fake, sync_interval_minutes=99999)
    cache._sync_interval_seconds = 0.02  # type: ignore[attr-defined]  # 加速触发
    fake.configure(*F, records=[Record("a", {"进度": "未分配"})])
    await cache.add_sheet(*F)
    captured: list[dict] = []

    async def on_changes(diffs):
        captured.append(diffs)

    cache.on_sync_changes = on_changes
    # 外部改动
    fake.records[F] = {"a": Record("a", {"进度": "已完成"})}
    await cache.start()
    # 轮询等回调被调用
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.01)
    await cache.stop()
    assert captured, "on_sync_changes 未被调用"
    assert F in captured[0]
    assert not captured[0][F].is_empty()


# =================================================================== periodic task lifecycle


async def test_start_then_stop_periodic_task(fake: FakeStorage) -> None:
    cache = SheetCache(fake, sync_interval_minutes=99999)
    await cache.start()
    assert cache._sync_task is not None  # type: ignore[attr-defined]
    assert not cache._sync_task.done()  # type: ignore[attr-defined,union-attr]
    await cache.stop()
    assert cache._sync_task is None  # type: ignore[attr-defined]


async def test_double_start_is_noop(fake: FakeStorage) -> None:
    cache = SheetCache(fake, sync_interval_minutes=99999)
    await cache.start()
    task = cache._sync_task  # type: ignore[attr-defined]
    await cache.start()
    assert cache._sync_task is task  # type: ignore[attr-defined]
    await cache.stop()
