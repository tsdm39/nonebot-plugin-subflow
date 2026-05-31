"""字幕组 Bot 业务核心。

把 cache（M2）、bindings、pipeline 三层组合成命令层能直接调的业务接口。
保持框架无关 —— 不依赖 NoneBot，便于单元测试和未来迁移其他 Bot 框架。

主要责任：
- 集数/分段的宽容匹配（D6 normalize_episode / normalize_segment）
- 集级写：create_episode / create_special（含 D10 流水线快照）
- 任务级写：claim_task / complete_task / abandon_task / set_in_progress / update_task
  全部在 cache.lock_for(...) 锁内做"读 → 校验 → 写"序列（D5）
- 依赖检查：predecessors 必须全部完成才能 claim；complete 后探测 newly_unlocked 下游
- 二次确认（D7）：prepare_delete / confirm_pending / cancel_pending；内存 dict + 懒过期
- 列表查询：list_episode / list_my_tasks / list_available
- 归档：archive_episode

业务结果通过 Outcome dataclass 返回，由命令层翻译成具体聊天消息（D3 提醒文案等）。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable

from .bindings import BindingStore
from .cache import SheetCache, SheetDiff
from .exceptions import StorageError
from .models import BindingEntry, Pipeline, PipelineStage, Record
from .pipeline import PipelineStore, downstream_of, predecessors_of, stage_names


# ============================================================ schema constants


COL_TYPE = "类型"
COL_EPISODE = "集数"
COL_SEGMENT = "分段"
COL_ASSIGNEE = "组员"
COL_PROGRESS = "进度"
COL_DONE_TIME = "完成时间"
COL_REMARK = "备注"

PROGRESS_UNASSIGNED = "未分配"
PROGRESS_ASSIGNED = "已分配"
PROGRESS_IN_PROGRESS = "进行中"
PROGRESS_DONE = "已完成"
PROGRESS_ARCHIVED = "归档"

SEGMENT_NONE = "0"  # D12：「不分段」字面值；替代旧的「全集」
SEGMENT_WHOLE = SEGMENT_NONE  # 兼容别名，老引用都指向 "0"

_TERMINAL_PROGRESS = {PROGRESS_DONE, PROGRESS_ARCHIVED}


# D17：外部（绕过 bot 直改表格）变更的语义动作类型
CHANGE_CLAIMED = "claimed"                    # 未分配 → 已分配
CHANGE_COMPLETED = "completed"                # → 已完成
CHANGE_ABANDONED = "abandoned"                # 已分配/进行中 → 未分配
CHANGE_IN_PROGRESS = "in_progress"            # 已分配 → 进行中
CHANGE_ARCHIVED = "archived"                  # → 归档
CHANGE_PROGRESS_REGRESSED = "progress_regressed"  # 其它非常规流转（如已完成回退）
CHANGE_ASSIGNEE_SET = "assignee_set"          # 进度未变，组员 空→X
CHANGE_ASSIGNEE_CLEARED = "assignee_cleared"  # 进度未变，组员 X→空
CHANGE_ASSIGNEE_CHANGED = "assignee_changed"  # 进度未变，组员 A→B
CHANGE_ROW_ADDED = "row_added"                # 直接新增整行
CHANGE_ROW_DELETED = "row_deleted"            # 直接删除整行


# ============================================================ business exceptions


class TaskError(Exception):
    """业务层错误的基类。"""


class TaskNotFoundError(TaskError):
    """指定的任务（番剧/集/工序/分段组合）在表里不存在。"""


class TaskAlreadyAssignedError(TaskError):
    """任务已被人接走。"""


class TaskNotAssignedError(TaskError):
    """任务还没被接，不能完成/放弃。"""


class PredecessorNotDoneError(TaskError):
    """前置工序还没全部完成。"""


class EpisodeAlreadyExistsError(TaskError):
    """该集已存在，拒绝重复创建。"""


class TooManyActiveTasksError(TaskError):
    """超过单人最大持有任务数。"""


class NoPendingConfirmationError(TaskError):
    """没有待确认的操作。"""


class ConfirmationExpiredError(TaskError):
    """确认窗口已过。"""


class SegmentMismatchError(TaskError):
    """新建集时分段参数与流水线不匹配（如有 segment=True 的工序却没传分段）。"""


# ============================================================ D6 / D12 normalization


_EPISODE_PREFIX = re.compile(r"^第\s*")
_EPISODE_SUFFIX = re.compile(r"\s*集$")


def normalize_episode(raw: str) -> str:
    """集数归一化：「第07集」/「7」/「07」/「OVA1」/「ova1」全部归到统一形式。

    规则：
      - 去前缀「第」
      - 去后缀「集」
      - 去前导零（全 0 字符串保留为 "0"）
      - 小写化
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    s = _EPISODE_PREFIX.sub("", s)
    s = _EPISODE_SUFFIX.sub("", s)
    s = s.strip().lower()
    # 去前导零仅对全数字（如 "07" → "7"，"0a" 不处理）
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def normalize_segment(raw: str) -> str:
    """D12 分段归一化：纯数字串去前导零；非数字小写化保留。

    - `None` / `""` → `""`
    - `"02"` → `"2"`；`"00"` → `"0"`；`"0"` → `"0"`
    - `"OP"` → `"op"`（实际分段不该出现字母，做防御）
    """
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    if not s:
        return ""
    if s.isdigit():
        return s.lstrip("0") or "0"
    return s


def _stage_is_segmented(pipeline: Pipeline, stage_name: str) -> bool:
    """从 pipeline 查 stage 是否标了 [分段]。stage 不在 pipeline 时返回 False。"""
    for s in pipeline:
        if s.stage == stage_name:
            return s.segment
    return False


def _episodes_eq(a: str, b: str) -> bool:
    return normalize_episode(a) == normalize_episode(b)


def _segments_eq(a: str, b: str) -> bool:
    return normalize_segment(a) == normalize_segment(b)


# ============================================================ outcome types


@dataclass(frozen=True)
class TaskRef:
    """业务层用的任务定位 — 已脱离 Record 内部表达，便于消息渲染。"""

    show: str
    episode: str
    stage: str
    segment: str  # 原样存储形态（如 "P1（0-8）" 或 "全集"）


@dataclass
class ClaimOutcome:
    task: Record
    ref: TaskRef


@dataclass
class CompleteOutcome:
    task: Record
    ref: TaskRef
    sender_qq: int
    original_assignee_raw: str  # 完成前的「组员」字段值（QQ 数字串 或 昵称）
    sender_was_assignee: bool
    same_stage_remaining: int       # 同集同工序剩余未完成的段数
    # D13：因此次完成而新解锁的「未分配」下游任务，元素是 (stage, segment) — segment="0" 表示不分段
    newly_unlocked_tasks: list[tuple[str, str]]
    # 兼容字段：上面 list 里去重后的 stage 名顺序
    newly_unlocked_stages: list[str]
    blocking_stages: list[str]      # 是下游、但仍因别的工序未完成而被阻塞
    # D17/Q8：因此次完成而对「已被接走的下游」变为可开工，元素 (stage, segment, assignee)
    newly_actionable_held: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class AbandonOutcome:
    task: Record
    ref: TaskRef
    sender_qq: int
    original_assignee_raw: str
    sender_was_assignee: bool


@dataclass
class InProgressOutcome:
    task: Record
    ref: TaskRef


@dataclass
class UpdateOutcome:
    task: Record
    ref: TaskRef
    changed_fields: dict[str, object]


@dataclass
class CreateEpisodeOutcome:
    show: str
    episode: str
    inserted: list[Record]
    initial_unlocked_stages: list[str]


@dataclass
class DeleteSummary:
    show: str
    episode: str
    matched: list[Record]
    expires_at: datetime
    overwrote_previous: bool


@dataclass
class DeleteOutcome:
    show: str
    episode: str
    deleted: list[Record]


@dataclass
class ArchiveOutcome:
    show: str
    episode: str
    archived: list[Record]
    skipped: list[Record]  # 不在「已完成」状态被略过的


# ============================================================ external change (D17)


@dataclass
class ExternalChange:
    """一条"人工绕过 bot 直改表格"的业务级变更（D17）。"""

    kind: str
    show: str
    episode: str
    stage: str
    segment: str
    assignee: str = ""       # 相关组员（新值；放弃/删除时为原组员）
    old_assignee: str = ""   # 仅 ASSIGNEE_CHANGED 用
    old_progress: str = ""   # 仅进度流转/回退展示用
    new_progress: str = ""


@dataclass
class ExternalChangeReport:
    """某番剧本轮同步检测到的全部外部变更 + 由此触发的下游解锁。"""

    show: str
    changes: list[ExternalChange] = field(default_factory=list)
    # 因外部完成而新解锁、且仍「未分配」的下游 — (episode, stage, segment)
    unlocked_unassigned: list[tuple[str, str, str]] = field(default_factory=list)
    # 因外部完成而新解锁、但已被人接走的下游 — (episode, stage, segment, assignee)
    unlocked_held: list[tuple[str, str, str, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.changes or self.unlocked_unassigned or self.unlocked_held
        )


# ============================================================ confirmation state (D7)


@dataclass
class _PendingConfirmation:
    binding: BindingEntry
    episode: str
    matched_record_ids: list[str]
    expires_at: datetime
    summary_text: str  # 留给命令层渲染时复用


# ============================================================ TaskManager


class TaskManager:
    def __init__(
        self,
        *,
        cache: SheetCache,
        bindings: BindingStore,
        pipelines: PipelineStore,
        max_tasks_per_user: int = 5,
        confirm_timeout_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._cache = cache
        self._bindings = bindings
        self._pipelines = pipelines
        self._max_tasks_per_user = max_tasks_per_user
        self._confirm_timeout = timedelta(seconds=confirm_timeout_seconds)
        self._clock = clock or datetime.now
        # D7: per-(group_id, user_qq) pending confirmation
        self._pending: dict[tuple[int, int], _PendingConfirmation] = {}

    # ================================================== creation

    async def create_episode(
        self,
        show: str,
        episode: str,
        segment_count: int = 1,
    ) -> CreateEpisodeOutcome:
        """新建一集所有任务（D10 同时快照流水线）。

        D12 格式：
        - 流水线里有 segment=True 的工序 → 各生成 segment_count 条记录，分段值为 "1".."N"
        - 流水线里 segment=False 的工序 → 只生成 1 条，分段值为 "0"
        - segment_count < 1 → SegmentMismatchError
        """
        if segment_count < 1:
            raise SegmentMismatchError(
                f"分段数必须 ≥ 1（当前 {segment_count}）"
            )
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        await self._ensure_no_episode_duplicate(binding, episode)
        pipeline = self._pipelines.get_pipeline(show)

        rows = self._expand_pipeline_rows(pipeline, episode, segment_count)
        if not rows:
            raise SegmentMismatchError("流水线为空，没有任何要生成的任务")

        inserted = await self._cache.add_records(
            binding.file_id, binding.sheet_id, rows
        )
        self._pipelines.snapshot_episode(show, episode, pipeline)
        initial_unlocked = [s.stage for s in pipeline if not s.depends_on]
        return CreateEpisodeOutcome(
            show=show,
            episode=episode,
            inserted=inserted,
            initial_unlocked_stages=initial_unlocked,
        )

    async def create_special(
        self,
        show: str,
        episode: str,
        stage_names_subset: list[str],
    ) -> CreateEpisodeOutcome:
        """OP/ED 等特殊集：用流水线的 depends_on 关系，但只生成指定工序，全部「全集」。

        当 subset 跳过了原流水线中的某些中间工序（如 OP 跳过 监制），用**传递闭包**
        把缺失的 pred 替换为它在 subset 内的最近祖先。例如：
          原流水线 校对→后期→监制→压制；subset 含 [校对, 压制]
          → 压制.depends_on 从 (监制,) 改写为 (校对,)
        这样新建特殊集后，工序仍按"事实上的顺序"流转，不会被 vacuous truth 误判为已解锁。
        """
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        if not stage_names_subset:
            raise SegmentMismatchError("/新建特殊 至少要指定一个工序")
        await self._ensure_no_episode_duplicate(binding, episode)

        current = self._pipelines.get_pipeline(show)
        subset_names = list(dict.fromkeys(stage_names_subset))
        subset_set = set(subset_names)
        current_by_name = {s.stage: s for s in current}

        def _closure(stage_name: str, visited: frozenset[str]) -> tuple[str, ...]:
            """返回 stage_name 在 subset 内的最近祖先；不存在则空。"""
            if stage_name in visited:
                return ()
            visited = visited | {stage_name}
            if stage_name in subset_set:
                return (stage_name,)
            stage = current_by_name.get(stage_name)
            if stage is None:
                return ()
            out: list[str] = []
            for p in stage.depends_on:
                for resolved in _closure(p, visited):
                    if resolved not in out:
                        out.append(resolved)
            return tuple(out)

        snapshot: Pipeline = []
        for name in subset_names:
            original = current_by_name.get(name)
            raw_deps = original.depends_on if original else ()
            new_deps: list[str] = []
            for p in raw_deps:
                for resolved in _closure(p, frozenset()):
                    if resolved != name and resolved not in new_deps:
                        new_deps.append(resolved)
            snapshot.append(
                PipelineStage(stage=name, segment=False, depends_on=tuple(new_deps))
            )

        rows = [
            {
                COL_TYPE: name,
                COL_EPISODE: episode,
                COL_SEGMENT: SEGMENT_NONE,
                COL_ASSIGNEE: "",
                COL_PROGRESS: PROGRESS_UNASSIGNED,
                COL_REMARK: "",
            }
            for name in subset_names
        ]
        inserted = await self._cache.add_records(
            binding.file_id, binding.sheet_id, rows
        )
        self._pipelines.snapshot_episode(show, episode, snapshot)
        initial_unlocked = [s.stage for s in snapshot if not s.depends_on]
        return CreateEpisodeOutcome(
            show=show,
            episode=episode,
            inserted=inserted,
            initial_unlocked_stages=initial_unlocked,
        )

    def _expand_pipeline_rows(
        self,
        pipeline: Pipeline,
        episode: str,
        segment_count: int,
    ) -> list[dict[str, object]]:
        """D12：分段工序各生成 segment_count 条（值 "1".."N"），
        不分段工序生成 1 条（值 "0"）。
        """
        if segment_count < 1:
            raise SegmentMismatchError(
                f"分段数必须 ≥ 1（当前 {segment_count}）"
            )
        rows: list[dict[str, object]] = []
        for stage in pipeline:
            if stage.segment:
                for i in range(1, segment_count + 1):
                    rows.append(
                        {
                            COL_TYPE: stage.stage,
                            COL_EPISODE: episode,
                            COL_SEGMENT: str(i),
                            COL_ASSIGNEE: "",
                            COL_PROGRESS: PROGRESS_UNASSIGNED,
                            COL_REMARK: "",
                        }
                    )
            else:
                rows.append(
                    {
                        COL_TYPE: stage.stage,
                        COL_EPISODE: episode,
                        COL_SEGMENT: SEGMENT_NONE,
                        COL_ASSIGNEE: "",
                        COL_PROGRESS: PROGRESS_UNASSIGNED,
                        COL_REMARK: "",
                    }
                )
        return rows

    async def _ensure_no_episode_duplicate(
        self, binding: BindingEntry, episode: str
    ) -> None:
        existing = self._cache.find_records(
            binding.file_id,
            binding.sheet_id,
            lambda r: _episodes_eq(r.values.get(COL_EPISODE, ""), episode),
        )
        if existing:
            raise EpisodeAlreadyExistsError(
                f"集数「{episode}」已存在 {len(existing)} 条记录，"
                f"请先 /删除任务 {binding.alias} {episode} 再重建"
            )

    # ================================================== task ops

    async def claim_task(
        self,
        show: str,
        episode: str,
        stage: str,
        segment: str | None,
        user_qq: int,
    ) -> ClaimOutcome:
        """D5 锁内: 校验 进度=未分配 + 用户配额 → 更新组员/进度。

        D15：接活**不再**校验前置工序——随时可提前认领；前置校验改在
        complete_task（/完成）里做。
        """
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        record = self._find_task(binding, episode, stage, segment)
        if record is None:
            raise TaskNotFoundError(
                f"找不到任务：{show} {episode} {stage} {segment or ''}"
            )
        async with self._cache.lock_for(
            binding.file_id, binding.sheet_id, record.record_id
        ):
            current = self._cache.get_record(
                binding.file_id, binding.sheet_id, record.record_id
            )
            if current is None:
                raise TaskNotFoundError("该任务在锁内已消失")
            if current.values.get(COL_PROGRESS) != PROGRESS_UNASSIGNED:
                holder = current.values.get(COL_ASSIGNEE) or "?"
                raise TaskAlreadyAssignedError(
                    f"任务已被 {holder} 接走（当前状态：{current.values.get(COL_PROGRESS)}）"
                )
            # 用户配额
            if self._max_tasks_per_user > 0:
                active = self._count_user_active(user_qq)
                if active >= self._max_tasks_per_user:
                    raise TooManyActiveTasksError(
                        f"你当前已持有 {active} 个未完成任务，达到上限 "
                        f"{self._max_tasks_per_user}，请先完成或放弃其中一些"
                    )
            updated = await self._cache.update_record(
                binding.file_id,
                binding.sheet_id,
                record.record_id,
                {
                    COL_ASSIGNEE: str(user_qq),
                    COL_PROGRESS: PROGRESS_ASSIGNED,
                },
            )
        return ClaimOutcome(
            task=updated,
            ref=self._make_ref(show, updated),
        )

    async def complete_task(
        self,
        show: str,
        episode: str,
        stage: str,
        segment: str | None,
        user_qq: int,
    ) -> CompleteOutcome:
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        record = self._find_task(binding, episode, stage, segment)
        if record is None:
            raise TaskNotFoundError(
                f"找不到任务：{show} {episode} {stage} {segment or ''}"
            )
        async with self._cache.lock_for(
            binding.file_id, binding.sheet_id, record.record_id
        ):
            current = self._cache.get_record(
                binding.file_id, binding.sheet_id, record.record_id
            )
            if current is None:
                raise TaskNotFoundError("该任务在锁内已消失")
            progress = current.values.get(COL_PROGRESS)
            if progress not in (PROGRESS_ASSIGNED, PROGRESS_IN_PROGRESS):
                raise TaskNotAssignedError(
                    f"任务当前状态是「{progress}」，无法标记完成"
                )

            # D15：前置工序校验（从 claim 移来）——前置没全完成不能完成。
            # D10：用集快照；D13：按本条 segment 走同段/全段依赖语义。
            # 完成前快照：同时用来探测下游 newly unlocked。
            pipeline = self._pipelines.get_episode_pipeline(show, episode)
            pre_records = self._episode_records(binding, episode)
            current_segment = str(current.values.get(COL_SEGMENT) or "")
            if not self._is_stage_unlocked(
                pipeline, pre_records, stage, segment=current_segment
            ):
                blockers = self._blocking_predecessors(
                    pipeline, pre_records, stage, segment=current_segment
                )
                raise PredecessorNotDoneError(
                    f"前置任务未完成，无法完成：{blockers}"
                )

            original_assignee = str(current.values.get(COL_ASSIGNEE) or "")
            sender_is_assignee = original_assignee == str(user_qq)

            updated = await self._cache.update_record(
                binding.file_id,
                binding.sheet_id,
                record.record_id,
                {
                    COL_PROGRESS: PROGRESS_DONE,
                    COL_DONE_TIME: self._clock(),
                },
            )

            post_records = self._episode_records(binding, episode)

            same_stage_remaining = sum(
                1
                for r in post_records
                if r.values.get(COL_TYPE) == stage
                and r.values.get(COL_PROGRESS) not in _TERMINAL_PROGRESS
            )

            # D13：per-(stage, segment) 粒度探测新解锁
            # D17/Q8：未分配下游 → 广播"可接"（newly_unlocked_tasks）；
            #         已被接走的下游 → @ 持有人"可开工"（newly_actionable_held）
            just_completed_segment = str(updated.values.get(COL_SEGMENT) or "")
            pred_segmented = _stage_is_segmented(pipeline, stage)
            newly_unlocked_tasks: list[tuple[str, str]] = []
            newly_actionable_held: list[tuple[str, str, str]] = []
            still_blocked: list[str] = []
            for downstream in downstream_of(pipeline, stage):
                ds_segmented = downstream.segment
                # 候选下游记录：尚未完成（未分配 / 已分配 / 进行中）的那些
                # - 同段-同段时仅检查与本次完成同段的那一条
                # - 否则检查该下游 stage 的所有候选
                candidates = [
                    r for r in post_records
                    if r.values.get(COL_TYPE) == downstream.stage
                    and r.values.get(COL_PROGRESS) not in _TERMINAL_PROGRESS
                ]
                if ds_segmented and pred_segmented:
                    candidates = [
                        r for r in candidates
                        if normalize_segment(r.values.get(COL_SEGMENT, ""))
                            == normalize_segment(just_completed_segment)
                    ]
                if not candidates:
                    continue
                stage_blocked = False
                for cand in candidates:
                    seg = str(cand.values.get(COL_SEGMENT) or "")
                    check_seg = seg if ds_segmented else None
                    was = self._is_stage_unlocked(
                        pipeline, pre_records, downstream.stage,
                        segment=check_seg,
                    )
                    is_now = self._is_stage_unlocked(
                        pipeline, post_records, downstream.stage,
                        segment=check_seg,
                    )
                    if not was and is_now:
                        if cand.values.get(COL_PROGRESS) == PROGRESS_UNASSIGNED:
                            newly_unlocked_tasks.append((downstream.stage, seg))
                        else:
                            holder = str(cand.values.get(COL_ASSIGNEE) or "")
                            newly_actionable_held.append(
                                (downstream.stage, seg, holder)
                            )
                    elif not is_now:
                        stage_blocked = True
                if stage_blocked and downstream.stage not in still_blocked:
                    still_blocked.append(downstream.stage)

            # 去重 stage 名（保持顺序）
            newly_unlocked_stage_names: list[str] = []
            for s, _ in newly_unlocked_tasks:
                if s not in newly_unlocked_stage_names:
                    newly_unlocked_stage_names.append(s)

        return CompleteOutcome(
            task=updated,
            ref=self._make_ref(show, updated),
            sender_qq=user_qq,
            original_assignee_raw=original_assignee,
            sender_was_assignee=sender_is_assignee,
            same_stage_remaining=same_stage_remaining,
            newly_unlocked_tasks=newly_unlocked_tasks,
            newly_unlocked_stages=newly_unlocked_stage_names,
            blocking_stages=still_blocked,
            newly_actionable_held=newly_actionable_held,
        )

    async def abandon_task(
        self,
        show: str,
        episode: str,
        stage: str,
        segment: str | None,
        user_qq: int,
    ) -> AbandonOutcome:
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        record = self._find_task(binding, episode, stage, segment)
        if record is None:
            raise TaskNotFoundError(
                f"找不到任务：{show} {episode} {stage} {segment or ''}"
            )
        async with self._cache.lock_for(
            binding.file_id, binding.sheet_id, record.record_id
        ):
            current = self._cache.get_record(
                binding.file_id, binding.sheet_id, record.record_id
            )
            if current is None:
                raise TaskNotFoundError("该任务在锁内已消失")
            progress = current.values.get(COL_PROGRESS)
            if progress not in (PROGRESS_ASSIGNED, PROGRESS_IN_PROGRESS):
                raise TaskNotAssignedError(
                    f"任务当前状态是「{progress}」，无法放弃"
                )
            original_assignee = str(current.values.get(COL_ASSIGNEE) or "")
            sender_is_assignee = original_assignee == str(user_qq)
            updated = await self._cache.update_record(
                binding.file_id,
                binding.sheet_id,
                record.record_id,
                {COL_ASSIGNEE: "", COL_PROGRESS: PROGRESS_UNASSIGNED},
            )
        return AbandonOutcome(
            task=updated,
            ref=self._make_ref(show, updated),
            sender_qq=user_qq,
            original_assignee_raw=original_assignee,
            sender_was_assignee=sender_is_assignee,
        )

    async def set_in_progress(
        self,
        show: str,
        episode: str,
        stage: str,
        segment: str | None,
        user_qq: int,
    ) -> InProgressOutcome:
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        record = self._find_task(binding, episode, stage, segment)
        if record is None:
            raise TaskNotFoundError(
                f"找不到任务：{show} {episode} {stage} {segment or ''}"
            )
        async with self._cache.lock_for(
            binding.file_id, binding.sheet_id, record.record_id
        ):
            current = self._cache.get_record(
                binding.file_id, binding.sheet_id, record.record_id
            )
            if current is None:
                raise TaskNotFoundError("该任务在锁内已消失")
            if current.values.get(COL_PROGRESS) != PROGRESS_ASSIGNED:
                raise TaskNotAssignedError(
                    f"任务当前状态是「{current.values.get(COL_PROGRESS)}」，"
                    f"只有「已分配」可以转为「进行中」"
                )
            updated = await self._cache.update_record(
                binding.file_id,
                binding.sheet_id,
                record.record_id,
                {COL_PROGRESS: PROGRESS_IN_PROGRESS},
            )
        return InProgressOutcome(task=updated, ref=self._make_ref(show, updated))

    async def update_task(
        self,
        show: str,
        episode: str,
        stage: str,
        segment: str | None,
        changes: dict[str, object],
    ) -> UpdateOutcome:
        """管理员的 /修改任务。任意列均可改；调用方应负责做权限和列名白名单。"""
        if not changes:
            raise TaskError("没有提供任何要修改的字段")
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        record = self._find_task(binding, episode, stage, segment)
        if record is None:
            raise TaskNotFoundError(
                f"找不到任务：{show} {episode} {stage} {segment or ''}"
            )
        async with self._cache.lock_for(
            binding.file_id, binding.sheet_id, record.record_id
        ):
            updated = await self._cache.update_record(
                binding.file_id, binding.sheet_id, record.record_id, changes
            )
        return UpdateOutcome(
            task=updated,
            ref=self._make_ref(show, updated),
            changed_fields=dict(changes),
        )

    # ================================================== delete with confirmation (D7)

    def prepare_delete(
        self,
        *,
        group_id: int,
        user_qq: int,
        show: str,
        episode: str,
        stage: str | None = None,
        segment: str | None = None,
    ) -> DeleteSummary:
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        matched = self._match_for_delete(binding, episode, stage, segment)
        if not matched:
            raise TaskNotFoundError(
                f"没找到匹配的任务：{show} {episode} {stage or ''} {segment or ''}"
            )
        now = self._clock()
        expires_at = now + self._confirm_timeout
        # 摘要文案
        summary = self._render_delete_summary(show, episode, matched)
        key = (group_id, user_qq)
        overwrote = key in self._pending
        self._pending[key] = _PendingConfirmation(
            binding=binding,
            episode=episode,
            matched_record_ids=[r.record_id for r in matched],
            expires_at=expires_at,
            summary_text=summary,
        )
        return DeleteSummary(
            show=show,
            episode=episode,
            matched=matched,
            expires_at=expires_at,
            overwrote_previous=overwrote,
        )

    async def confirm_pending(
        self, *, group_id: int, user_qq: int
    ) -> DeleteOutcome:
        key = (group_id, user_qq)
        pending = self._pending.get(key)
        if pending is None:
            raise NoPendingConfirmationError(
                "没有待确认的操作（可能已超时或被覆盖）"
            )
        if self._clock() > pending.expires_at:
            del self._pending[key]
            raise ConfirmationExpiredError(
                f"确认窗口已过（{pending.expires_at:%H:%M:%S} 之前有效），请重新发起"
            )
        # 真删
        binding = pending.binding
        # 读出 record 列表（用于返回，给消息层）
        deleted_records: list[Record] = []
        for rid in pending.matched_record_ids:
            rec = self._cache.get_record(binding.file_id, binding.sheet_id, rid)
            if rec is not None:
                deleted_records.append(rec)
        await self._cache.delete_records(
            binding.file_id,
            binding.sheet_id,
            pending.matched_record_ids,
        )
        del self._pending[key]
        # 如果整集都删光，把流水线快照也清掉（避免污染未来同名集）
        remaining = self._cache.find_records(
            binding.file_id,
            binding.sheet_id,
            lambda r: _episodes_eq(r.values.get(COL_EPISODE, ""), pending.episode),
        )
        if not remaining:
            self._pipelines.remove_episode_snapshot(binding.alias, pending.episode)
        return DeleteOutcome(
            show=binding.alias,
            episode=pending.episode,
            deleted=deleted_records,
        )

    def cancel_pending(self, *, group_id: int, user_qq: int) -> bool:
        return self._pending.pop((group_id, user_qq), None) is not None

    def has_pending(self, *, group_id: int, user_qq: int) -> bool:
        pending = self._pending.get((group_id, user_qq))
        if pending is None:
            return False
        if self._clock() > pending.expires_at:
            # 懒过期：访问时发现过期就清掉
            del self._pending[(group_id, user_qq)]
            return False
        return True

    def _match_for_delete(
        self,
        binding: BindingEntry,
        episode: str,
        stage: str | None,
        segment: str | None,
    ) -> list[Record]:
        return self._cache.find_records(
            binding.file_id,
            binding.sheet_id,
            lambda r: (
                _episodes_eq(r.values.get(COL_EPISODE, ""), episode)
                and (stage is None or r.values.get(COL_TYPE) == stage)
                and (
                    segment is None
                    or _segments_eq(r.values.get(COL_SEGMENT, ""), segment)
                )
            ),
        )

    def _render_delete_summary(
        self, show: str, episode: str, matched: list[Record]
    ) -> str:
        active = [
            r
            for r in matched
            if r.values.get(COL_PROGRESS)
            in (PROGRESS_ASSIGNED, PROGRESS_IN_PROGRESS)
        ]
        lines = [
            f"⚠️ 即将删除：{show} 第{episode}集 共 {len(matched)} 条记录"
        ]
        if active:
            details = "、".join(
                f"{r.values.get(COL_TYPE)}{r.values.get(COL_SEGMENT) or ''}"
                f"(@{r.values.get(COL_ASSIGNEE)})"
                for r in active
            )
            lines.append(f"  其中 {len(active)} 条已分配/进行中：{details}")
        lines.append(
            f"回复「确认删除」执行，{int(self._confirm_timeout.total_seconds())}秒内有效"
        )
        return "\n".join(lines)

    # ================================================== archive

    async def archive_episode(
        self, show: str, episode: str
    ) -> ArchiveOutcome:
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        episode_records = self._cache.find_records(
            binding.file_id,
            binding.sheet_id,
            lambda r: _episodes_eq(r.values.get(COL_EPISODE, ""), episode),
        )
        if not episode_records:
            raise TaskNotFoundError(f"该集没有任何记录：{show} {episode}")

        archived: list[Record] = []
        skipped: list[Record] = []
        for rec in episode_records:
            if rec.values.get(COL_PROGRESS) != PROGRESS_DONE:
                skipped.append(rec)
                continue
            async with self._cache.lock_for(
                binding.file_id, binding.sheet_id, rec.record_id
            ):
                updated = await self._cache.update_record(
                    binding.file_id,
                    binding.sheet_id,
                    rec.record_id,
                    {COL_PROGRESS: PROGRESS_ARCHIVED},
                )
                archived.append(updated)
        return ArchiveOutcome(
            show=show, episode=episode, archived=archived, skipped=skipped
        )

    # ================================================== queries (零 API)

    def list_episode(self, show: str, episode: str) -> list[Record]:
        binding = self._bindings.get(show)
        if binding is None:
            from .exceptions import AliasNotFoundError
            raise AliasNotFoundError(f"番剧「{show}」未绑定")
        return self._episode_records(binding, episode)

    def list_my_tasks(
        self, user_qq: int, show_filter: Iterable[str] | None = None
    ) -> list[tuple[str, Record]]:
        """返回 [(show_alias, record), ...]：当前用户名下未完成的所有任务。"""
        user_str = str(user_qq)
        out: list[tuple[str, Record]] = []
        for entry in self._bindings.list_all():
            if show_filter is not None and entry.alias not in show_filter:
                continue
            for rec in self._cache.get_records(entry.file_id, entry.sheet_id):
                if rec.values.get(COL_ASSIGNEE) != user_str:
                    continue
                if rec.values.get(COL_PROGRESS) in _TERMINAL_PROGRESS:
                    continue
                out.append((entry.alias, rec))
        return out

    def list_available(
        self, show_filter: Iterable[str] | None = None
    ) -> list[tuple[str, Record]]:
        """列出所有 进度=未分配 的任务。

        D15：接活不再校验前置（可提前认领），故 /待接 直接列出全部未分配，
        不再按前置是否满足过滤。
        """
        out: list[tuple[str, Record]] = []
        for entry in self._bindings.list_all():
            if show_filter is not None and entry.alias not in show_filter:
                continue
            for rec in self._cache.get_records(entry.file_id, entry.sheet_id):
                if rec.values.get(COL_PROGRESS) == PROGRESS_UNASSIGNED:
                    out.append((entry.alias, rec))
        return out

    # ================================================== external change (D17)

    def interpret_external_changes(
        self, show: str, diff: SheetDiff
    ) -> ExternalChangeReport:
        """把 cache 的 SheetDiff 解释成有业务含义的外部变更（D17）。纯读，不写回。

        - 改行 → 语义动作（接活/完成/放弃/进行中/归档/指派/清空组员/进度回退）；
          仅备注/集数/分段/类型变化（进度+组员都没变）→ 丢弃。
        - 增/删整行 → ROW_ADDED / ROW_DELETED。
        - 本轮新变为终态（已完成/归档）的工序 → 触发下游解锁探测（同段/全段），
          解锁的下游按当前进度分流：未分配→广播可接；已被接走→@ 持有人。
        """
        report = ExternalChangeReport(show=show)

        for rec in diff.added:
            report.changes.append(self._row_change(show, CHANGE_ROW_ADDED, rec))
        for rec in diff.removed:
            report.changes.append(
                self._row_change(show, CHANGE_ROW_DELETED, rec)
            )

        # episode → 本轮新变为终态的工序名集合
        completed_by_ep: dict[str, set[str]] = {}
        for old_rec, new_rec in diff.changed:
            change = self._interpret_changed(show, old_rec, new_rec)
            if change is not None:
                report.changes.append(change)
            old_p = old_rec.values.get(COL_PROGRESS)
            new_p = new_rec.values.get(COL_PROGRESS)
            if new_p in _TERMINAL_PROGRESS and old_p not in _TERMINAL_PROGRESS:
                ep = str(new_rec.values.get(COL_EPISODE, ""))
                completed_by_ep.setdefault(ep, set()).add(
                    str(new_rec.values.get(COL_TYPE, ""))
                )

        binding = self._bindings.get(show)
        if binding is not None:
            for ep, completed_stages in completed_by_ep.items():
                self._collect_external_unlocks(
                    show, binding, ep, completed_stages, report
                )
        return report

    def _row_change(
        self, show: str, kind: str, rec: Record
    ) -> ExternalChange:
        return ExternalChange(
            kind=kind,
            show=show,
            episode=str(rec.values.get(COL_EPISODE, "")),
            stage=str(rec.values.get(COL_TYPE, "")),
            segment=str(rec.values.get(COL_SEGMENT, "")),
            assignee=str(rec.values.get(COL_ASSIGNEE) or ""),
            new_progress=str(rec.values.get(COL_PROGRESS) or ""),
        )

    def _interpret_changed(
        self, show: str, old_rec: Record, new_rec: Record
    ) -> ExternalChange | None:
        old_p = str(old_rec.values.get(COL_PROGRESS) or "")
        new_p = str(new_rec.values.get(COL_PROGRESS) or "")
        old_a = str(old_rec.values.get(COL_ASSIGNEE) or "")
        new_a = str(new_rec.values.get(COL_ASSIGNEE) or "")
        base = dict(
            show=show,
            episode=str(new_rec.values.get(COL_EPISODE, "")),
            stage=str(new_rec.values.get(COL_TYPE, "")),
            segment=str(new_rec.values.get(COL_SEGMENT, "")),
        )
        if new_p != old_p:
            if new_p == PROGRESS_DONE:
                kind = CHANGE_COMPLETED
            elif new_p == PROGRESS_ARCHIVED:
                kind = CHANGE_ARCHIVED
            elif new_p == PROGRESS_IN_PROGRESS and old_p == PROGRESS_ASSIGNED:
                kind = CHANGE_IN_PROGRESS
            elif new_p == PROGRESS_ASSIGNED and old_p == PROGRESS_UNASSIGNED:
                return ExternalChange(
                    kind=CHANGE_CLAIMED, assignee=new_a,
                    old_progress=old_p, new_progress=new_p, **base
                )
            elif new_p == PROGRESS_UNASSIGNED and old_p in (
                PROGRESS_ASSIGNED, PROGRESS_IN_PROGRESS
            ):
                return ExternalChange(
                    kind=CHANGE_ABANDONED, assignee=old_a,
                    old_progress=old_p, new_progress=new_p, **base
                )
            else:
                kind = CHANGE_PROGRESS_REGRESSED
            return ExternalChange(
                kind=kind, assignee=(new_a or old_a),
                old_progress=old_p, new_progress=new_p, **base
            )
        # 进度未变 → 看组员
        if new_a != old_a:
            if not old_a:
                return ExternalChange(
                    kind=CHANGE_ASSIGNEE_SET, assignee=new_a, **base
                )
            if not new_a:
                return ExternalChange(
                    kind=CHANGE_ASSIGNEE_CLEARED, assignee=old_a, **base
                )
            return ExternalChange(
                kind=CHANGE_ASSIGNEE_CHANGED,
                assignee=new_a, old_assignee=old_a, **base
            )
        # 进度、组员都没变 → 仅备注/集数/分段/类型变化 → 不在范围
        return None

    def _collect_external_unlocks(
        self,
        show: str,
        binding: BindingEntry,
        episode: str,
        completed_stages: set[str],
        report: ExternalChangeReport,
    ) -> None:
        """对本集"本轮新完成的工序"探测新解锁的下游（复用 _is_stage_unlocked）。

        只迭代刚完成工序的下游，故"该下游确因本轮完成而解锁"天然成立；
        _is_stage_unlocked 再保证同段/全段语义和"全部前置都满足"。
        """
        pipeline = self._pipelines.get_episode_pipeline(show, episode)
        post_records = self._episode_records(binding, episode)
        seen_unassigned: set[tuple[str, str]] = set()
        seen_held: set[tuple[str, str, str]] = set()
        for cstage in completed_stages:
            for downstream in downstream_of(pipeline, cstage):
                ds_segmented = downstream.segment
                candidates = [
                    r for r in post_records
                    if r.values.get(COL_TYPE) == downstream.stage
                    and r.values.get(COL_PROGRESS) not in _TERMINAL_PROGRESS
                ]
                for cand in candidates:
                    seg = str(cand.values.get(COL_SEGMENT) or "")
                    check_seg = seg if ds_segmented else None
                    if not self._is_stage_unlocked(
                        pipeline, post_records, downstream.stage,
                        segment=check_seg,
                    ):
                        continue
                    if cand.values.get(COL_PROGRESS) == PROGRESS_UNASSIGNED:
                        key = (downstream.stage, seg)
                        if key not in seen_unassigned:
                            seen_unassigned.add(key)
                            report.unlocked_unassigned.append(
                                (episode, downstream.stage, seg)
                            )
                    else:
                        holder = str(cand.values.get(COL_ASSIGNEE) or "")
                        hkey = (downstream.stage, seg, holder)
                        if hkey not in seen_held:
                            seen_held.add(hkey)
                            report.unlocked_held.append(
                                (episode, downstream.stage, seg, holder)
                            )

    # ================================================== internal helpers

    def _episode_records(
        self, binding: BindingEntry, episode: str
    ) -> list[Record]:
        return self._cache.find_records(
            binding.file_id,
            binding.sheet_id,
            lambda r: _episodes_eq(r.values.get(COL_EPISODE, ""), episode),
        )

    def _find_task(
        self,
        binding: BindingEntry,
        episode: str,
        stage: str,
        segment: str | None,
    ) -> Record | None:
        candidates = self._cache.find_records(
            binding.file_id,
            binding.sheet_id,
            lambda r: (
                _episodes_eq(r.values.get(COL_EPISODE, ""), episode)
                and r.values.get(COL_TYPE) == stage
            ),
        )
        if not candidates:
            return None
        if segment is None:
            # 不分段工序通常只有 1 条（"全集"）；若多条则要求显式给段名
            if len(candidates) == 1:
                return candidates[0]
            return None
        for rec in candidates:
            if _segments_eq(rec.values.get(COL_SEGMENT, ""), segment):
                return rec
        return None

    def _is_stage_unlocked(
        self,
        pipeline: Pipeline,
        episode_records: list[Record],
        stage_name: str,
        segment: str | None = None,
    ) -> bool:
        """D13：检查指定 (stage, segment) 是否所有前置完成。

        - 当下游和某条前置**都是 [分段]** 工序，且 segment 给了 →
          按"同段"匹配（只看相同 segment 的前置记录）
        - 否则（任一不分段，或不指定 segment）→ 按"全段完成"匹配（旧行为）
        - 前置工序在本集没有任何记录 → vacuous truth，视为满足
        """
        downstream_segmented = _stage_is_segmented(pipeline, stage_name)
        for p in predecessors_of(pipeline, stage_name):
            p_records = [
                r for r in episode_records if r.values.get(COL_TYPE) == p
            ]
            if not p_records:
                continue
            pred_segmented = _stage_is_segmented(pipeline, p)
            if downstream_segmented and pred_segmented and segment is not None:
                target = normalize_segment(segment)
                same_seg = [
                    r for r in p_records
                    if normalize_segment(r.values.get(COL_SEGMENT, "")) == target
                ]
                if not same_seg:
                    continue  # 该段在前置不存在（异常分段数错配）→ 视为满足
                if any(
                    r.values.get(COL_PROGRESS) not in _TERMINAL_PROGRESS
                    for r in same_seg
                ):
                    return False
            else:
                if any(
                    r.values.get(COL_PROGRESS) not in _TERMINAL_PROGRESS
                    for r in p_records
                ):
                    return False
        return True

    def _blocking_predecessors(
        self,
        pipeline: Pipeline,
        episode_records: list[Record],
        stage_name: str,
        segment: str | None = None,
    ) -> list[str]:
        """返回阻塞当前 (stage, segment) 的前置描述列表。
        分段-分段同段匹配时返回 "翻译 1" 这种带段号的；其它返回"翻译"。"""
        out: list[str] = []
        downstream_segmented = _stage_is_segmented(pipeline, stage_name)
        for p in predecessors_of(pipeline, stage_name):
            p_records = [
                r for r in episode_records if r.values.get(COL_TYPE) == p
            ]
            if not p_records:
                continue
            pred_segmented = _stage_is_segmented(pipeline, p)
            if downstream_segmented and pred_segmented and segment is not None:
                target = normalize_segment(segment)
                relevant = [
                    r for r in p_records
                    if normalize_segment(r.values.get(COL_SEGMENT, "")) == target
                ]
                if relevant and any(
                    r.values.get(COL_PROGRESS) not in _TERMINAL_PROGRESS
                    for r in relevant
                ):
                    out.append(f"{p} {segment}")
            else:
                if any(
                    r.values.get(COL_PROGRESS) not in _TERMINAL_PROGRESS
                    for r in p_records
                ):
                    out.append(p)
        return out

    def _count_user_active(self, user_qq: int) -> int:
        user_str = str(user_qq)
        count = 0
        for entry in self._bindings.list_all():
            for rec in self._cache.get_records(entry.file_id, entry.sheet_id):
                if (
                    rec.values.get(COL_ASSIGNEE) == user_str
                    and rec.values.get(COL_PROGRESS) not in _TERMINAL_PROGRESS
                ):
                    count += 1
        return count

    def _make_ref(self, show: str, record: Record) -> TaskRef:
        return TaskRef(
            show=show,
            episode=str(record.values.get(COL_EPISODE, "")),
            stage=str(record.values.get(COL_TYPE, "")),
            segment=str(record.values.get(COL_SEGMENT, "")),
        )
