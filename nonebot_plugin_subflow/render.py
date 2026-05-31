"""消息渲染层。

把 task_manager 的 Outcome 翻译成 NoneBot Message（含 @ 段）。
所有渲染是纯函数，方便单元测试。
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .models import BindingEntry, Pipeline, Record
from .pipeline import to_dsl
from .task_manager import (
    CHANGE_ABANDONED,
    CHANGE_ARCHIVED,
    CHANGE_ASSIGNEE_CHANGED,
    CHANGE_ASSIGNEE_CLEARED,
    CHANGE_ASSIGNEE_SET,
    CHANGE_CLAIMED,
    CHANGE_COMPLETED,
    CHANGE_IN_PROGRESS,
    CHANGE_PROGRESS_REGRESSED,
    CHANGE_ROW_ADDED,
    CHANGE_ROW_DELETED,
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
    SEGMENT_NONE,
    AbandonOutcome,
    ArchiveOutcome,
    ClaimOutcome,
    CompleteOutcome,
    CreateEpisodeOutcome,
    DeleteOutcome,
    DeleteSummary,
    ExternalChange,
    ExternalChangeReport,
    InProgressOutcome,
    UpdateOutcome,
)


# ============================================================ low-level helpers


def assignee_segment(raw: str | None) -> MessageSegment:
    """组员字段 → MessageSegment。纯数字 QQ → @；其他 → 文本。"""
    if raw and str(raw).isdigit():
        return MessageSegment.at(int(raw))
    return MessageSegment.text(raw or "?")


def _task_label(ref) -> str:
    """形如「淡岛百景7 翻译 2」或「淡岛百景7 校对」（不分段省略 segment）。"""
    base = f"{ref.show}{ref.episode} {ref.stage}"
    if ref.segment and ref.segment != SEGMENT_NONE:
        return f"{base} {ref.segment}"
    return base


def _segment_sort_key(seg: str) -> int:
    """同工序的分段按 int 排序，避免 '10' 排在 '2' 前面。"""
    try:
        return int(seg)
    except (TypeError, ValueError):
        return 0


_PROGRESS_ICON = {
    PROGRESS_UNASSIGNED: "⚪",
    PROGRESS_ASSIGNED: "🟡",
    PROGRESS_IN_PROGRESS: "🔵",
    PROGRESS_DONE: "✅",
    PROGRESS_ARCHIVED: "📦",
}


# ============================================================ task-level


def render_claim(outcome: ClaimOutcome) -> Message:
    msg = Message()
    msg += assignee_segment(outcome.task.values.get(COL_ASSIGNEE))
    msg += MessageSegment.text(f" 已接 {_task_label(outcome.ref)} ✅")
    return msg


def render_complete(outcome: CompleteOutcome) -> Message:
    """D3：非组员完成时附带提醒；下游解锁时附带可接通知。"""
    msg = Message()
    msg += assignee_segment(str(outcome.sender_qq))
    msg += MessageSegment.text(f" 完成了 {_task_label(outcome.ref)} ✅")

    # D3 提醒
    if not outcome.sender_was_assignee and outcome.original_assignee_raw:
        msg += MessageSegment.text("\n⚠️ ")
        if outcome.original_assignee_raw.isdigit():
            msg += MessageSegment.text("你不是该任务的当前组员（")
            msg += assignee_segment(outcome.original_assignee_raw)
            msg += MessageSegment.text("），已为你标记完成")
        else:
            msg += MessageSegment.text(
                f"此任务原本由「{outcome.original_assignee_raw}」承担"
            )

    # 同工序剩余
    if outcome.same_stage_remaining > 0:
        msg += MessageSegment.text(
            f"\n{outcome.ref.stage} 还剩 {outcome.same_stage_remaining} 个分段未完成"
        )

    # 下游解锁 — D13：精确到 (stage, segment) 粒度
    # 同 stage 多段一起解锁时合并成一行避免刷屏
    by_stage: dict[str, list[str]] = {}
    order: list[str] = []
    for stage, seg in outcome.newly_unlocked_tasks:
        if stage not in by_stage:
            by_stage[stage] = []
            order.append(stage)
        by_stage[stage].append(seg)
    for stage in order:
        segs = by_stage[stage]
        if len(segs) == 1 and segs[0] != SEGMENT_NONE:
            label = f"{stage} {segs[0]}"
            cmd = f"/接活 {outcome.ref.show} {outcome.ref.episode} {stage} {segs[0]}"
        elif len(segs) == 1 and segs[0] == SEGMENT_NONE:
            label = stage
            cmd = f"/接活 {outcome.ref.show} {outcome.ref.episode} {stage}"
        else:
            # 多段同时解锁
            seg_list = "/".join(segs)
            label = f"{stage} {seg_list}"
            cmd = f"/接活 {outcome.ref.show} {outcome.ref.episode} {stage} <段号>"
        msg += MessageSegment.text(
            f"\n🎉 {label} 现在可以接了 → {cmd}"
        )

    # D17/Q8：已被接走的下游解锁 → @ 持有人提示可开工
    for stage, seg, holder in outcome.newly_actionable_held:
        held_label = stage if (not seg or seg == SEGMENT_NONE) else f"{stage} {seg}"
        msg += MessageSegment.text("\n")
        msg += assignee_segment(holder)
        msg += MessageSegment.text(
            f" 你的 {outcome.ref.show}{outcome.ref.episode} {held_label} "
            f"前置已完成，可以开始了 ▶️"
        )

    # 仍阻塞的下游（信息性）
    if (
        outcome.blocking_stages
        and not outcome.newly_unlocked_tasks
        and not outcome.newly_actionable_held
    ):
        if outcome.same_stage_remaining == 0:
            blockers = "/".join(outcome.blocking_stages)
            msg += MessageSegment.text(
                f"\n{outcome.ref.stage} 已全部完成 ✅ "
                f"等待其它前置完成后 {blockers} 即可开始"
            )

    return msg


def render_abandon(outcome: AbandonOutcome) -> Message:
    msg = Message()
    msg += assignee_segment(str(outcome.sender_qq))
    msg += MessageSegment.text(f" 放弃了 {_task_label(outcome.ref)}，已重新可接")
    if not outcome.sender_was_assignee and outcome.original_assignee_raw:
        msg += MessageSegment.text("\n⚠️ ")
        if outcome.original_assignee_raw.isdigit():
            msg += MessageSegment.text("你不是该任务的当前组员（")
            msg += assignee_segment(outcome.original_assignee_raw)
            msg += MessageSegment.text("）")
        else:
            msg += MessageSegment.text(
                f"此任务原本由「{outcome.original_assignee_raw}」承担"
            )
    return msg


def render_in_progress(outcome: InProgressOutcome) -> Message:
    msg = Message()
    msg += assignee_segment(outcome.task.values.get(COL_ASSIGNEE))
    msg += MessageSegment.text(f" 开始 {_task_label(outcome.ref)} 🔵")
    return msg


def render_update(outcome: UpdateOutcome) -> Message:
    changes = "、".join(f"{k}={v}" for k, v in outcome.changed_fields.items())
    return Message(MessageSegment.text(
        f"已更新 {_task_label(outcome.ref)}：{changes} ✅"
    ))


# ============================================================ episode-level


def render_create_episode(outcome: CreateEpisodeOutcome) -> str:
    """新建集后的公告文。"""
    # 按工序统计
    by_type: dict[str, int] = {}
    for rec in outcome.inserted:
        by_type[rec.values.get(COL_TYPE)] = by_type.get(rec.values.get(COL_TYPE), 0) + 1
    parts = "  ".join(
        f"{t}×{n}" if n > 1 else t for t, n in by_type.items()
    )
    unlocked = "、".join(outcome.initial_unlocked_stages)
    return (
        f"{outcome.show} 第{outcome.episode}集 任务已创建 "
        f"({len(outcome.inserted)} 条：{parts})\n"
        f"当前可接：{unlocked}"
    )


def render_delete_summary(summary: DeleteSummary) -> Message:
    """删除确认摘要。D16：已分配明细里的组员用 CQ 艾特码，故返回 Message。"""
    msg = Message()
    msg += MessageSegment.text(
        f"⚠️ 即将删除：{summary.show} 第{summary.episode}集 "
        f"共 {len(summary.matched)} 条记录"
    )
    active = [
        r
        for r in summary.matched
        if r.values.get(COL_PROGRESS) in (PROGRESS_ASSIGNED, PROGRESS_IN_PROGRESS)
    ]
    if active:
        msg += MessageSegment.text(f"\n  其中 {len(active)} 条已分配/进行中：")
        for i, r in enumerate(active):
            if i > 0:
                msg += MessageSegment.text("、")
            label = f"{r.values.get(COL_TYPE)}{r.values.get(COL_SEGMENT) or ''}"
            msg += MessageSegment.text(f"{label}(")
            msg += assignee_segment(r.values.get(COL_ASSIGNEE))
            msg += MessageSegment.text(")")
    if summary.overwrote_previous:
        msg += MessageSegment.text("\n（已覆盖你之前的待确认操作）")
    until = summary.expires_at.strftime("%H:%M:%S")
    msg += MessageSegment.text(f"\n回复「确认删除」执行，{until} 之前有效")
    return msg


def render_delete_done(outcome: DeleteOutcome) -> str:
    return f"已删除 {outcome.show} 第{outcome.episode}集 共 {len(outcome.deleted)} 条记录 ✅"


def render_archive(outcome: ArchiveOutcome) -> str:
    parts = [
        f"{outcome.show} 第{outcome.episode}集 归档完成："
        f"{len(outcome.archived)} 条 已完成 → 归档"
    ]
    if outcome.skipped:
        parts.append(f"另有 {len(outcome.skipped)} 条非已完成状态，已跳过")
    return "\n".join(parts)


# ============================================================ progress board


def render_progress(show: str, episode: str, records: list[Record]) -> Message:
    """`/进度 番剧 集数` 的展板：按工序+分段排版。

    D16：组员用 CQ 艾特码（数字 QQ → 真实 @），不再是字面量文本，故返回 Message。
    """
    if not records:
        return Message(
            MessageSegment.text(f"📺 {show} 第{episode}集 暂无任务记录")
        )

    msg = Message()
    msg += MessageSegment.text(f"📺 {show} 第{episode}集 进度一览\n" + "─" * 22)
    # 排序键：(类型出现顺序, 分段名)
    type_order: dict[str, int] = {}
    for r in records:
        t = r.values.get(COL_TYPE, "")
        if t not in type_order:
            type_order[t] = len(type_order)
    records_sorted = sorted(
        records,
        key=lambda r: (
            type_order.get(r.values.get(COL_TYPE, ""), 999),
            _segment_sort_key(r.values.get(COL_SEGMENT, "")),
        ),
    )
    for r in records_sorted:
        progress = r.values.get(COL_PROGRESS, PROGRESS_UNASSIGNED)
        icon = _PROGRESS_ICON.get(progress, "❓")
        label = r.values.get(COL_TYPE, "?")
        segment = r.values.get(COL_SEGMENT, "")
        if segment and segment != SEGMENT_NONE:
            label = f"{label} {segment}"
        msg += MessageSegment.text(f"\n{icon} {progress}  {label}")
        assignee = r.values.get(COL_ASSIGNEE) or ""
        if assignee:
            msg += MessageSegment.text("  ")
            msg += assignee_segment(assignee)
        if progress == PROGRESS_DONE and r.values.get(COL_DONE_TIME):
            done = r.values[COL_DONE_TIME]
            done_str = (
                done.strftime("%m-%d") if isinstance(done, datetime) else str(done)
            )
            msg += MessageSegment.text(f"  {done_str}")
    return msg


# ============================================================ list / bindings


def render_my_tasks(user_qq: int, tasks: list[tuple[str, Record]]) -> Message:
    """D16：抬头 @自己用 CQ 艾特码，故返回 Message。"""
    if not tasks:
        return Message(MessageSegment.text("你当前没有未完成的任务 🎉"))
    msg = Message()
    msg += MessageSegment.at(user_qq)
    msg += MessageSegment.text(f" 名下未完成的 {len(tasks)} 个任务：")
    for show, rec in tasks:
        icon = _PROGRESS_ICON.get(
            rec.values.get(COL_PROGRESS, ""), "•"
        )
        seg = rec.values.get(COL_SEGMENT, "")
        seg_part = f" {seg}" if seg and seg != SEGMENT_NONE else ""
        msg += MessageSegment.text(
            f"\n  {icon} {show}{rec.values.get(COL_EPISODE)} "
            f"{rec.values.get(COL_TYPE)}{seg_part}"
        )
    return msg


def render_available(tasks: list[tuple[str, Record]]) -> str:
    if not tasks:
        return "暂无可接任务 — 都被抢完了 / 等上游"
    lines = [f"共 {len(tasks)} 个可接任务："]
    for show, rec in tasks:
        seg = rec.values.get(COL_SEGMENT, "")
        seg_part = f" {seg}" if seg and seg != SEGMENT_NONE else ""
        cmd = (
            f"/接活 {show} {rec.values.get(COL_EPISODE)} "
            f"{rec.values.get(COL_TYPE)}{seg_part}".rstrip()
        )
        lines.append(f"  ⚪ {cmd}")
    return "\n".join(lines)


def render_bindings_list(
    entries: list[BindingEntry],
    *,
    title: str = "本群绑定的番剧",
) -> str:
    if not entries:
        return f"{title}：（空）"
    lines = [f"{title}（共 {len(entries)} 项）："]
    for e in entries:
        lines.append(
            f"  • {e.alias}  群={e.group_id}  "
            f"file={e.file_id}  sheet={e.sheet_id}"
        )
    return "\n".join(lines)


def render_pipeline_view(show: str, pipeline: Pipeline, is_default: bool) -> str:
    dsl = to_dsl(pipeline)
    suffix = "（使用默认流水线）" if is_default else "（自定义流水线）"
    return f"{show} 工序链 {suffix}：\n  {dsl}"


# ============================================================ external changes (D17)


def _full_label(show: str, episode: str, stage: str, segment: str) -> str:
    """形如「淡岛百景07 翻译 1」；不分段省略 segment。"""
    base = f"{show}{episode} {stage}"
    if segment and segment != SEGMENT_NONE:
        return f"{base} {segment}"
    return base


def _external_change_line(ch: ExternalChange) -> Message:
    """把一条 ExternalChange 渲染成一行（一个 Message，含 @ 段）。"""
    label = _full_label(ch.show, ch.episode, ch.stage, ch.segment)
    msg = Message()
    if ch.kind == CHANGE_CLAIMED:
        msg += assignee_segment(ch.assignee)
        msg += MessageSegment.text(f" 接走了 {label}")
    elif ch.kind == CHANGE_COMPLETED:
        if ch.assignee:
            msg += assignee_segment(ch.assignee)
            msg += MessageSegment.text(f" 完成了 {label} ✅")
        else:
            msg += MessageSegment.text(f"{label} 已完成 ✅")
    elif ch.kind == CHANGE_ABANDONED:
        msg += MessageSegment.text(f"{label} 被放弃")
        if ch.assignee:
            msg += MessageSegment.text("（原 ")
            msg += assignee_segment(ch.assignee)
            msg += MessageSegment.text("）")
        msg += MessageSegment.text("，重新可接")
    elif ch.kind == CHANGE_IN_PROGRESS:
        if ch.assignee:
            msg += assignee_segment(ch.assignee)
            msg += MessageSegment.text(f" 开始了 {label} 🔵")
        else:
            msg += MessageSegment.text(f"{label} 进行中 🔵")
    elif ch.kind == CHANGE_ARCHIVED:
        msg += MessageSegment.text(f"{label} 已归档 📦")
    elif ch.kind == CHANGE_PROGRESS_REGRESSED:
        msg += MessageSegment.text(
            f"{label} 进度被改为「{ch.new_progress}」（原「{ch.old_progress}」）"
        )
    elif ch.kind == CHANGE_ASSIGNEE_SET:
        msg += MessageSegment.text(f"{label} 组员被填为 ")
        msg += assignee_segment(ch.assignee)
    elif ch.kind == CHANGE_ASSIGNEE_CLEARED:
        msg += MessageSegment.text(f"{label} 组员被清空（原 ")
        msg += assignee_segment(ch.assignee)
        msg += MessageSegment.text("）")
    elif ch.kind == CHANGE_ASSIGNEE_CHANGED:
        msg += MessageSegment.text(f"{label} 组员由 ")
        msg += assignee_segment(ch.old_assignee)
        msg += MessageSegment.text(" 改为 ")
        msg += assignee_segment(ch.assignee)
    elif ch.kind == CHANGE_ROW_ADDED:
        unassigned = ch.new_progress in ("", PROGRESS_UNASSIGNED)
        suffix = "（可接）" if unassigned else f"（{ch.new_progress}）"
        msg += MessageSegment.text(f"新增任务：{label}{suffix}")
    elif ch.kind == CHANGE_ROW_DELETED:
        msg += MessageSegment.text(f"任务被删除：{label}")
        if ch.assignee:
            msg += MessageSegment.text("（原 ")
            msg += assignee_segment(ch.assignee)
            msg += MessageSegment.text("）")
    else:  # 兜底
        msg += MessageSegment.text(f"{label} 发生变更")
    return msg


def _unlock_unassigned_line(show: str, episode: str, stage: str, seg: str) -> Message:
    if seg and seg != SEGMENT_NONE:
        label = f"{stage} {seg}"
        cmd = f"/接活 {show} {episode} {stage} {seg}"
    else:
        label = stage
        cmd = f"/接活 {show} {episode} {stage}"
    return Message(MessageSegment.text(f"🎉 {label} 现在可以接了 → {cmd}"))


def _unlock_held_line(
    show: str, episode: str, stage: str, seg: str, holder: str
) -> Message:
    label = _full_label(show, episode, stage, seg)
    msg = Message()
    msg += assignee_segment(holder)
    msg += MessageSegment.text(f" 你的 {label} 前置已完成，可以开始了 ▶️")
    return msg


def render_external_changes(
    report: ExternalChangeReport, *, digest_threshold: int = 5
) -> list[Message]:
    """D17：把外部变更报告渲染成群消息。

    本群本轮总行数 ≤ digest_threshold → 逐条发（每条一条 Message，可单独 @）；
    > digest_threshold → 合并成一条多行汇总，防刷屏。空报告返回 []。
    """
    lines: list[Message] = [
        _external_change_line(ch) for ch in report.changes
    ]
    for episode, stage, seg in report.unlocked_unassigned:
        lines.append(_unlock_unassigned_line(report.show, episode, stage, seg))
    for episode, stage, seg, holder in report.unlocked_held:
        lines.append(_unlock_held_line(report.show, episode, stage, seg, holder))

    if not lines:
        return []
    if len(lines) <= digest_threshold:
        out: list[Message] = []
        for ln in lines:
            m = Message(MessageSegment.text("📝 "))
            m += ln
            out.append(m)
        return out
    digest = Message(
        MessageSegment.text(
            f"📝 检测到「{report.show}」表格被直接修改 {len(lines)} 处："
        )
    )
    for ln in lines:
        digest += MessageSegment.text("\n")
        digest += ln
    return [digest]
