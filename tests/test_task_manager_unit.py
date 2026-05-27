"""task_manager.py 单元测试 — 用 FakeStorage 跑全部业务路径。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nonebot_plugin_subflow.bindings import BindingStore
from nonebot_plugin_subflow.cache import SheetCache
from nonebot_plugin_subflow.exceptions import AliasNotFoundError
from nonebot_plugin_subflow.pipeline import PipelineStore, parse_dsl
from nonebot_plugin_subflow.task_manager import (
    COL_ASSIGNEE,
    COL_DONE_TIME,
    COL_EPISODE,
    COL_PROGRESS,
    COL_REMARK,
    COL_SEGMENT,
    COL_TYPE,
    PROGRESS_ARCHIVED,
    PROGRESS_ASSIGNED,
    PROGRESS_DONE,
    PROGRESS_IN_PROGRESS,
    PROGRESS_UNASSIGNED,
    ConfirmationExpiredError,
    EpisodeAlreadyExistsError,
    NoPendingConfirmationError,
    PredecessorNotDoneError,
    SEGMENT_NONE,
    SegmentMismatchError,
    TaskAlreadyAssignedError,
    TaskManager,
    TaskNotAssignedError,
    TaskNotFoundError,
    TooManyActiveTasksError,
    normalize_episode,
    normalize_segment,
)

from .test_cache_unit import FakeStorage


SHOW = "淡岛百景"
ALIAS_GROUP = 222222222
DEFAULT_DSL = "翻译[分段],时轴[分段] → 校对 → 后期 → 监制 → 压制"


@pytest.fixture
def setup(tmp_path: Path):
    """完整脚手架：FakeStorage + cache + bindings + pipelines。"""
    fake = FakeStorage()
    cache = SheetCache(fake, sync_interval_minutes=99999)
    bindings = BindingStore.load(tmp_path / "bindings.json", main_group_id=111)
    binding = bindings.bind(
        group_id=ALIAS_GROUP,
        alias=SHOW,
        file_id="F",
        sheet_id="S",
        bound_by=987,
    )
    pipelines = PipelineStore.load(
        config_path=tmp_path / "pipelines.json",
        snapshot_path=tmp_path / "episode_pipelines.json",
        default_pipeline_dsl=DEFAULT_DSL,
    )
    return {
        "fake": fake,
        "cache": cache,
        "bindings": bindings,
        "binding": binding,
        "pipelines": pipelines,
    }


@pytest.fixture
def tm(setup):
    """默认 TaskManager，max=5。"""
    return TaskManager(
        cache=setup["cache"],
        bindings=setup["bindings"],
        pipelines=setup["pipelines"],
        max_tasks_per_user=5,
        confirm_timeout_seconds=30,
    )


# ============================================================ D6 normalization


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("07", "7"),
        ("7", "7"),
        ("第07集", "7"),
        ("第7集", "7"),
        ("OVA1", "ova1"),
        ("ova1", "ova1"),
        ("OP", "op"),
        ("0", "0"),
        ("00", "0"),
        ("  07 ", "7"),
        ("", ""),
    ],
)
def test_normalize_episode(raw, expected) -> None:
    assert normalize_episode(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1", "1"),
        ("02", "2"),
        ("0", "0"),
        ("00", "0"),
        ("10", "10"),
        ("  3  ", "3"),
        ("", ""),
        (None, ""),
        # 防御：非数字进来也不崩
        ("op", "op"),
        ("OP", "op"),
    ],
)
def test_normalize_segment(raw, expected) -> None:
    assert normalize_segment(raw) == expected


# ============================================================ create_episode


async def test_create_episode_expands_pipeline(tm: TaskManager, setup) -> None:
    """D12：/新建集 X 7 3 → 翻译/时轴各 3 段（'1'/'2'/'3'），其它工序 1 条 '0'。"""
    outcome = await tm.create_episode(SHOW, "07", 3)
    # 6 工序：翻译×3 + 时轴×3 + 校对 + 后期 + 监制 + 压制 = 10 条
    assert len(outcome.inserted) == 10
    by_type: dict[str, list] = {}
    for r in outcome.inserted:
        by_type.setdefault(r.values[COL_TYPE], []).append(r)
    assert len(by_type["翻译"]) == 3
    assert len(by_type["时轴"]) == 3
    assert len(by_type["校对"]) == 1
    assert by_type["校对"][0].values[COL_SEGMENT] == SEGMENT_NONE  # "0"
    seg_labels = {r.values[COL_SEGMENT] for r in by_type["翻译"]}
    assert seg_labels == {"1", "2", "3"}
    assert set(outcome.initial_unlocked_stages) == {"翻译", "时轴"}
    assert setup["pipelines"].has_snapshot(SHOW, "07")


async def test_create_episode_segment_count_one(tm: TaskManager) -> None:
    """单段：翻译/时轴各 1 条（'1'），其它仍 '0'。"""
    outcome = await tm.create_episode(SHOW, "07", 1)
    by_type: dict[str, list] = {}
    for r in outcome.inserted:
        by_type.setdefault(r.values[COL_TYPE], []).append(r)
    assert len(by_type["翻译"]) == 1
    assert by_type["翻译"][0].values[COL_SEGMENT] == "1"
    assert by_type["校对"][0].values[COL_SEGMENT] == "0"


async def test_create_episode_segment_count_default_is_one(tm: TaskManager) -> None:
    outcome = await tm.create_episode(SHOW, "07")
    by_type: dict[str, list] = {}
    for r in outcome.inserted:
        by_type.setdefault(r.values[COL_TYPE], []).append(r)
    assert len(by_type["翻译"]) == 1


async def test_create_episode_rejects_duplicate(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    with pytest.raises(EpisodeAlreadyExistsError):
        await tm.create_episode(SHOW, "7", 1)  # 归一化后等于 07


async def test_create_episode_rejects_zero_segment_count(
    tm: TaskManager,
) -> None:
    with pytest.raises(SegmentMismatchError):
        await tm.create_episode(SHOW, "07", 0)


async def test_create_episode_unknown_show_raises(tm: TaskManager) -> None:
    with pytest.raises(AliasNotFoundError):
        await tm.create_episode("不存在的番剧", "07", 1)


# ============================================================ create_special


async def test_create_special_only_specified_stages(
    tm: TaskManager, setup
) -> None:
    outcome = await tm.create_special(SHOW, "OP", ["翻译", "时轴", "校对", "压制"])
    assert len(outcome.inserted) == 4
    types = {r.values[COL_TYPE] for r in outcome.inserted}
    assert types == {"翻译", "时轴", "校对", "压制"}
    # D12：所有特殊任务分段均为 "0"
    assert all(r.values[COL_SEGMENT] == SEGMENT_NONE for r in outcome.inserted)
    # 初始可接：翻译 + 时轴（depends_on 是空）
    assert set(outcome.initial_unlocked_stages) == {"翻译", "时轴"}
    # 快照：4 个工序，深度关系做传递闭包替换：
    # 校对.depends_on=(翻译,时轴) 都在 subset → 保留
    # 压制.depends_on=(监制,) 监制不在 → 沿 监制→后期→校对 找到 subset 内的祖先 → (校对,)
    snap = setup["pipelines"].get_episode_pipeline(SHOW, "OP")
    assert len(snap) == 4
    snap_by_name = {s.stage: s for s in snap}
    assert snap_by_name["校对"].depends_on == ("翻译", "时轴")
    assert snap_by_name["压制"].depends_on == ("校对",)


async def test_create_special_empty_stages_raises(tm: TaskManager) -> None:
    with pytest.raises(SegmentMismatchError):
        await tm.create_special(SHOW, "OP", [])


async def test_create_special_compress_cannot_be_claimed_until_proof(
    tm: TaskManager,
) -> None:
    """传递闭包效果：OP=[翻译, 时轴, 校对, 压制]，没完校对前压制不能接。"""
    await tm.create_special(SHOW, "OP", ["翻译", "时轴", "校对", "压制"])
    with pytest.raises(PredecessorNotDoneError):
        await tm.claim_task(SHOW, "OP", "压制", None, user_qq=100)
    # 把上游 3 个全完成后，压制才能接
    for stage in ("翻译", "时轴", "校对"):
        await tm.claim_task(SHOW, "OP", stage, None, user_qq=100)
        await tm.complete_task(SHOW, "OP", stage, None, user_qq=100)
    outcome = await tm.claim_task(SHOW, "OP", "压制", None, user_qq=100)
    assert outcome.task.values[COL_TYPE] == "压制"


# ============================================================ claim_task


async def test_claim_task_happy_path(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    outcome = await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    assert outcome.task.values[COL_ASSIGNEE] == "100"
    assert outcome.task.values[COL_PROGRESS] == PROGRESS_ASSIGNED
    assert outcome.ref.episode == "07"
    assert outcome.ref.stage == "翻译"


async def test_claim_task_with_normalized_episode_and_segment(
    tm: TaskManager,
) -> None:
    await tm.create_episode(SHOW, "07", 1)
    # 用户用 "7" 和 "01" 也能匹配上（数字前导零）
    outcome = await tm.claim_task(SHOW, "7", "翻译", "01", user_qq=100)
    assert outcome.task.values[COL_ASSIGNEE] == "100"


async def test_claim_task_already_assigned_raises(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    with pytest.raises(TaskAlreadyAssignedError):
        await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=200)


async def test_claim_task_predecessor_not_done_raises(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    # 校对 依赖 翻译 + 时轴，都没完成
    with pytest.raises(PredecessorNotDoneError):
        await tm.claim_task(SHOW, "07", "校对", None, user_qq=100)


async def test_claim_task_unknown_segment_raises(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    with pytest.raises(TaskNotFoundError):
        await tm.claim_task(SHOW, "07", "翻译", "99", user_qq=100)


async def test_claim_task_unsegmented_can_omit_segment(tm: TaskManager) -> None:
    # 完成翻译/时轴所有分段后，校对（不分段）应能用 segment=None 接
    await tm.create_episode(SHOW, "07", 1)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    await tm.complete_task(SHOW, "07", "翻译", "1", user_qq=100)
    await tm.claim_task(SHOW, "07", "时轴", "1", user_qq=100)
    await tm.complete_task(SHOW, "07", "时轴", "1", user_qq=100)
    outcome = await tm.claim_task(SHOW, "07", "校对", None, user_qq=100)
    assert outcome.task.values[COL_TYPE] == "校对"


async def test_claim_task_max_tasks_enforced(setup, tm: TaskManager) -> None:
    """限制 max=2 时接到第 3 个抛错。"""
    tm = TaskManager(
        cache=setup["cache"],
        bindings=setup["bindings"],
        pipelines=setup["pipelines"],
        max_tasks_per_user=2,
        confirm_timeout_seconds=30,
    )
    await tm.create_episode(SHOW, "07", 3)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    await tm.claim_task(SHOW, "07", "翻译", "2", user_qq=100)
    with pytest.raises(TooManyActiveTasksError):
        await tm.claim_task(SHOW, "07", "翻译", "3", user_qq=100)


async def test_claim_task_concurrent_only_one_wins(tm: TaskManager) -> None:
    """D5 全链路验证：同时两人 /接活 → 一成一败。"""
    await tm.create_episode(SHOW, "07", 1)

    async def claim(user: int) -> str:
        try:
            await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=user)
            return f"{user}-ok"
        except TaskAlreadyAssignedError:
            return f"{user}-busy"

    results = await asyncio.gather(claim(100), claim(200))
    statuses = sorted(r.split("-")[1] for r in results)
    assert statuses == ["busy", "ok"]


# ============================================================ complete_task


async def test_complete_task_sets_terminal_state_and_time(
    setup, tm: TaskManager
) -> None:
    await tm.create_episode(SHOW, "07", 1)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    fixed_time = datetime(2026, 5, 26, 18, 0, 0)
    tm._clock = lambda: fixed_time  # 注入固定时间
    outcome = await tm.complete_task(SHOW, "07", "翻译", "1", user_qq=100)
    assert outcome.task.values[COL_PROGRESS] == PROGRESS_DONE
    assert outcome.task.values[COL_DONE_TIME] == fixed_time
    assert outcome.sender_was_assignee
    assert outcome.original_assignee_raw == "100"


async def test_complete_task_by_non_assignee_records_warning(
    tm: TaskManager,
) -> None:
    """D3：非组员也能 /完成，但 outcome 标记给命令层加提醒。"""
    await tm.create_episode(SHOW, "07", 1)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    outcome = await tm.complete_task(SHOW, "07", "翻译", "1", user_qq=999)
    assert outcome.sender_was_assignee is False
    assert outcome.original_assignee_raw == "100"


async def test_complete_task_reports_same_stage_remaining(
    tm: TaskManager,
) -> None:
    await tm.create_episode(
        SHOW, "07", 3
    )
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    outcome = await tm.complete_task(SHOW, "07", "翻译", "1", user_qq=100)
    # 翻译 P2 / P3 还没做 → remaining 2
    assert outcome.same_stage_remaining == 2
    assert outcome.newly_unlocked_stages == []  # 翻译还没全完 → 校对没解锁


async def test_complete_task_unlocks_downstream_only_when_all_predecessors_done(
    tm: TaskManager,
) -> None:
    """翻译3段 + 时轴3段都做完 → 校对解锁；只完成翻译不解锁。"""
    await tm.create_episode(SHOW, "07", 1)
    # 翻译P1完成
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    o1 = await tm.complete_task(SHOW, "07", "翻译", "1", user_qq=100)
    # 翻译全做完了（只有 P1） → 但时轴没动 → 校对仍阻塞
    assert "校对" not in o1.newly_unlocked_stages
    assert "校对" in o1.blocking_stages

    # 时轴P1完成
    await tm.claim_task(SHOW, "07", "时轴", "1", user_qq=200)
    o2 = await tm.complete_task(SHOW, "07", "时轴", "1", user_qq=200)
    # 现在所有前置完成 → 校对应解锁
    assert "校对" in o2.newly_unlocked_stages


async def test_complete_task_wrong_state_raises(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    # 没接就完成
    with pytest.raises(TaskNotAssignedError):
        await tm.complete_task(SHOW, "07", "翻译", "1", user_qq=100)


# ============================================================ abandon


async def test_abandon_clears_assignee(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    outcome = await tm.abandon_task(SHOW, "07", "翻译", "1", user_qq=100)
    assert outcome.task.values[COL_PROGRESS] == PROGRESS_UNASSIGNED
    assert outcome.task.values[COL_ASSIGNEE] == ""
    assert outcome.sender_was_assignee


async def test_abandon_by_non_assignee_works_with_warning_flag(
    tm: TaskManager,
) -> None:
    await tm.create_episode(SHOW, "07", 1)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    outcome = await tm.abandon_task(SHOW, "07", "翻译", "1", user_qq=999)
    assert outcome.sender_was_assignee is False
    assert outcome.original_assignee_raw == "100"


async def test_abandon_unassigned_task_raises(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    with pytest.raises(TaskNotAssignedError):
        await tm.abandon_task(SHOW, "07", "翻译", "1", user_qq=100)


# ============================================================ set_in_progress


async def test_set_in_progress_transitions(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    outcome = await tm.set_in_progress(SHOW, "07", "翻译", "1", user_qq=100)
    assert outcome.task.values[COL_PROGRESS] == PROGRESS_IN_PROGRESS


async def test_set_in_progress_from_wrong_state_raises(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    with pytest.raises(TaskNotAssignedError):  # 未分配状态
        await tm.set_in_progress(SHOW, "07", "翻译", "1", user_qq=100)


# ============================================================ delete with confirmation (D7)


async def test_prepare_then_confirm_deletes_records(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    summary = tm.prepare_delete(
        group_id=ALIAS_GROUP, user_qq=987, show=SHOW, episode="07"
    )
    assert len(summary.matched) == 6  # 翻译 + 时轴 + 4 unsegmented
    assert not summary.overwrote_previous
    outcome = await tm.confirm_pending(group_id=ALIAS_GROUP, user_qq=987)
    assert len(outcome.deleted) == 6
    # 缓存里也没了
    assert tm.list_episode(SHOW, "07") == []


async def test_prepare_delete_with_stage_filter(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 2)
    summary = tm.prepare_delete(
        group_id=ALIAS_GROUP, user_qq=987, show=SHOW, episode="07", stage="翻译"
    )
    assert len(summary.matched) == 2  # 翻译 P1 + P2


async def test_prepare_delete_no_match_raises(tm: TaskManager) -> None:
    with pytest.raises(TaskNotFoundError):
        tm.prepare_delete(
            group_id=ALIAS_GROUP, user_qq=987, show=SHOW, episode="99"
        )


async def test_confirm_without_pending_raises(tm: TaskManager) -> None:
    with pytest.raises(NoPendingConfirmationError):
        await tm.confirm_pending(group_id=ALIAS_GROUP, user_qq=987)


async def test_confirm_after_timeout_raises(tm: TaskManager) -> None:
    """D7：用注入 clock 测懒过期。"""
    await tm.create_episode(SHOW, "07", 1)
    base_time = datetime(2026, 5, 26, 18, 0, 0)
    tm._clock = lambda: base_time
    tm.prepare_delete(
        group_id=ALIAS_GROUP, user_qq=987, show=SHOW, episode="07"
    )
    # 假装时间已过 31 秒
    tm._clock = lambda: base_time + timedelta(seconds=31)
    with pytest.raises(ConfirmationExpiredError):
        await tm.confirm_pending(group_id=ALIAS_GROUP, user_qq=987)
    # 记录还在（未删除）
    assert len(tm.list_episode(SHOW, "07")) == 6


async def test_second_prepare_overwrites_first(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    await tm.create_episode(SHOW, "08", 1)
    s1 = tm.prepare_delete(
        group_id=ALIAS_GROUP, user_qq=987, show=SHOW, episode="07"
    )
    assert not s1.overwrote_previous
    s2 = tm.prepare_delete(
        group_id=ALIAS_GROUP, user_qq=987, show=SHOW, episode="08"
    )
    assert s2.overwrote_previous
    # 确认时执行的是后发的（删 08）
    outcome = await tm.confirm_pending(group_id=ALIAS_GROUP, user_qq=987)
    assert outcome.episode == "08"
    # 07 还在
    assert len(tm.list_episode(SHOW, "07")) > 0


async def test_pending_isolated_per_user_and_group(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    tm.prepare_delete(
        group_id=ALIAS_GROUP, user_qq=987, show=SHOW, episode="07"
    )
    # 另一人在同群发起 confirm 应失败
    with pytest.raises(NoPendingConfirmationError):
        await tm.confirm_pending(group_id=ALIAS_GROUP, user_qq=998)


async def test_full_episode_delete_clears_snapshot(tm: TaskManager, setup) -> None:
    await tm.create_episode(SHOW, "07", 1)
    assert setup["pipelines"].has_snapshot(SHOW, "07")
    tm.prepare_delete(
        group_id=ALIAS_GROUP, user_qq=987, show=SHOW, episode="07"
    )
    await tm.confirm_pending(group_id=ALIAS_GROUP, user_qq=987)
    assert not setup["pipelines"].has_snapshot(SHOW, "07")


# ============================================================ archive


async def test_archive_only_done_tasks(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    await tm.complete_task(SHOW, "07", "翻译", "1", user_qq=100)
    outcome = await tm.archive_episode(SHOW, "07")
    assert len(outcome.archived) == 1
    assert outcome.archived[0].values[COL_TYPE] == "翻译"
    assert outcome.archived[0].values[COL_PROGRESS] == PROGRESS_ARCHIVED
    # 其余 5 条没动
    assert len(outcome.skipped) == 5


async def test_archive_nonexistent_episode_raises(tm: TaskManager) -> None:
    with pytest.raises(TaskNotFoundError):
        await tm.archive_episode(SHOW, "999")


# ============================================================ list queries


async def test_list_my_tasks_returns_active_only(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    await tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    await tm.claim_task(SHOW, "07", "时轴", "1", user_qq=100)
    await tm.complete_task(SHOW, "07", "时轴", "1", user_qq=100)
    mine = tm.list_my_tasks(100)
    # 1 个未完成（翻译P1），时轴P1 已完成被过滤掉
    assert len(mine) == 1
    show_alias, rec = mine[0]
    assert show_alias == SHOW
    assert rec.values[COL_TYPE] == "翻译"


async def test_list_available_filters_blocked_stages(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    avail = tm.list_available()
    # 只有「翻译P1」和「时轴P1」可接（其他工序前置未满足）
    types = {rec.values[COL_TYPE] for _, rec in avail}
    assert types == {"翻译", "时轴"}


# ============================================================ update_task


async def test_update_task_changes_arbitrary_field(tm: TaskManager) -> None:
    await tm.create_episode(SHOW, "07", 1)
    outcome = await tm.update_task(
        SHOW, "07", "翻译", "1", {COL_REMARK: "加急"}
    )
    assert outcome.task.values[COL_REMARK] == "加急"
    assert outcome.changed_fields == {COL_REMARK: "加急"}


# ============================================================ D13: 同段串行依赖（arrow DSL）


ARROW_DSL = "翻译[分段] → 时轴[分段] → 校对 → 后期 → 监制 → 压制"


@pytest.fixture
def arrow_setup(tmp_path: Path):
    fake = FakeStorage()
    cache = SheetCache(fake, sync_interval_minutes=99999)
    bindings = BindingStore.load(tmp_path / "bindings.json", main_group_id=111)
    binding = bindings.bind(
        group_id=ALIAS_GROUP, alias=SHOW, file_id="F", sheet_id="S", bound_by=987
    )
    pipelines = PipelineStore.load(
        config_path=tmp_path / "pipelines.json",
        snapshot_path=tmp_path / "episode_pipelines.json",
        default_pipeline_dsl=ARROW_DSL,
    )
    return {"fake": fake, "cache": cache, "bindings": bindings, "binding": binding, "pipelines": pipelines}


@pytest.fixture
def arrow_tm(arrow_setup):
    return TaskManager(
        cache=arrow_setup["cache"],
        bindings=arrow_setup["bindings"],
        pipelines=arrow_setup["pipelines"],
        max_tasks_per_user=99,
        confirm_timeout_seconds=30,
    )


async def test_d13_per_segment_unlock_only_matching_segment(
    arrow_tm: TaskManager,
) -> None:
    """完成 翻译 1 → 时轴 1 解锁（仅本段）；时轴 2/3 仍阻塞。"""
    await arrow_tm.create_episode(SHOW, "07", 3)
    await arrow_tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    outcome = await arrow_tm.complete_task(SHOW, "07", "翻译", "1", user_qq=100)
    assert ("时轴", "1") in outcome.newly_unlocked_tasks
    # 时轴 2/3 不在 newly_unlocked，因为对应的 翻译 2/3 还没完成
    unlocked_pairs = set(outcome.newly_unlocked_tasks)
    assert ("时轴", "2") not in unlocked_pairs
    assert ("时轴", "3") not in unlocked_pairs


async def test_d13_claim_segmented_blocked_by_same_segment_only(
    arrow_tm: TaskManager,
) -> None:
    """试图接 时轴 2，但 翻译 2 没完 → blocked；与 翻译 1/3 状态无关。"""
    await arrow_tm.create_episode(SHOW, "07", 3)
    # 完成 翻译 1（不应影响 时轴 2 的依赖）
    await arrow_tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    await arrow_tm.complete_task(SHOW, "07", "翻译", "1", user_qq=100)
    # 接 时轴 2 应失败 — 因为 翻译 2 未完成
    with pytest.raises(PredecessorNotDoneError) as exc:
        await arrow_tm.claim_task(SHOW, "07", "时轴", "2", user_qq=200)
    # 错误信息应含段号（"翻译 2"）
    assert "翻译 2" in str(exc.value)


async def test_d13_claim_segmented_works_when_same_segment_complete(
    arrow_tm: TaskManager,
) -> None:
    """翻译 2 完成后，时轴 2 可接，但 时轴 3 仍 blocked。"""
    await arrow_tm.create_episode(SHOW, "07", 3)
    # 完成 翻译 2
    await arrow_tm.claim_task(SHOW, "07", "翻译", "2", user_qq=100)
    await arrow_tm.complete_task(SHOW, "07", "翻译", "2", user_qq=100)
    # 时轴 2 应能接
    outcome = await arrow_tm.claim_task(SHOW, "07", "时轴", "2", user_qq=200)
    assert outcome.task.values[COL_SEGMENT] == "2"
    # 时轴 3 仍 blocked
    with pytest.raises(PredecessorNotDoneError):
        await arrow_tm.claim_task(SHOW, "07", "时轴", "3", user_qq=200)


async def test_d13_segmented_to_unsegmented_uses_all_done(
    arrow_tm: TaskManager,
) -> None:
    """翻译[分段] → 时轴[分段] → 校对（不分段）：校对要等全部时轴段完才解锁。"""
    await arrow_tm.create_episode(SHOW, "07", 2)
    # 全部翻译/时轴完成
    for seg in ("1", "2"):
        await arrow_tm.claim_task(SHOW, "07", "翻译", seg, user_qq=100)
        await arrow_tm.complete_task(SHOW, "07", "翻译", seg, user_qq=100)
    # 完成 时轴 1 — 校对应仍 blocked
    await arrow_tm.claim_task(SHOW, "07", "时轴", "1", user_qq=200)
    o1 = await arrow_tm.complete_task(SHOW, "07", "时轴", "1", user_qq=200)
    assert ("校对", "0") not in o1.newly_unlocked_tasks
    assert "校对" in o1.blocking_stages
    # 完成 时轴 2 — 校对终于解锁
    await arrow_tm.claim_task(SHOW, "07", "时轴", "2", user_qq=300)
    o2 = await arrow_tm.complete_task(SHOW, "07", "时轴", "2", user_qq=300)
    assert ("校对", "0") in o2.newly_unlocked_tasks


async def test_d13_list_available_filters_by_same_segment(
    arrow_tm: TaskManager,
) -> None:
    """list_available 应正确按段过滤：初始只有翻译三段可接，时轴全 blocked。"""
    await arrow_tm.create_episode(SHOW, "07", 3)
    avail = arrow_tm.list_available()
    by_type: dict[str, list[str]] = {}
    for _, rec in avail:
        by_type.setdefault(rec.values[COL_TYPE], []).append(rec.values[COL_SEGMENT])
    assert sorted(by_type.get("翻译", [])) == ["1", "2", "3"]
    assert "时轴" not in by_type  # 全 blocked

    # 完成 翻译 2 → 时轴 2 应进入 available
    await arrow_tm.claim_task(SHOW, "07", "翻译", "2", user_qq=100)
    await arrow_tm.complete_task(SHOW, "07", "翻译", "2", user_qq=100)
    avail2 = arrow_tm.list_available()
    timing_segs = sorted(
        rec.values[COL_SEGMENT]
        for _, rec in avail2
        if rec.values[COL_TYPE] == "时轴"
    )
    assert timing_segs == ["2"]


async def test_d13_unsegmented_pipeline_unaffected(arrow_tm: TaskManager) -> None:
    """单段集（segment_count=1）：行为与旧版本等价 — 翻译 1 完成解锁 时轴 1。"""
    await arrow_tm.create_episode(SHOW, "07", 1)
    await arrow_tm.claim_task(SHOW, "07", "翻译", "1", user_qq=100)
    o = await arrow_tm.complete_task(SHOW, "07", "翻译", "1", user_qq=100)
    assert ("时轴", "1") in o.newly_unlocked_tasks
