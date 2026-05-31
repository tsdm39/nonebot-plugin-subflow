"""render.py 单元测试 — 纯函数，不需要起 NoneBot。"""

from __future__ import annotations

from datetime import datetime

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from nonebot_plugin_subflow.models import BindingEntry, Record
from nonebot_plugin_subflow.pipeline import parse_dsl
from nonebot_plugin_subflow.render import (
    assignee_segment,
    render_abandon,
    render_archive,
    render_available,
    render_bindings_list,
    render_claim,
    render_complete,
    render_create_episode,
    render_delete_done,
    render_delete_summary,
    render_external_changes,
    render_in_progress,
    render_my_tasks,
    render_pipeline_view,
    render_progress,
    render_update,
)
from nonebot_plugin_subflow.task_manager import (
    CHANGE_CLAIMED,
    CHANGE_COMPLETED,
    CHANGE_ROW_ADDED,
    COL_ASSIGNEE,
    COL_DONE_TIME,
    COL_EPISODE,
    COL_PROGRESS,
    COL_REMARK,
    COL_SEGMENT,
    COL_TYPE,
    PROGRESS_ASSIGNED,
    PROGRESS_DONE,
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
    TaskRef,
    UpdateOutcome,
)


# ============================================================ assignee_segment


def test_assignee_segment_qq_number_becomes_at() -> None:
    seg = assignee_segment("12345")
    assert seg.type == "at"
    # OneBot v11 把 qq 存为字符串
    assert str(seg.data["qq"]) == "12345"


def test_assignee_segment_nickname_becomes_text() -> None:
    seg = assignee_segment("小明同学")
    assert seg.type == "text"
    assert seg.data["text"] == "小明同学"


def test_assignee_segment_empty_returns_question() -> None:
    seg = assignee_segment("")
    assert seg.type == "text"
    assert seg.data["text"] == "?"


# ============================================================ task ops


def _make_record(**values) -> Record:
    defaults = {
        COL_TYPE: "翻译",
        COL_EPISODE: "07",
        COL_SEGMENT: "1",
        COL_ASSIGNEE: "100",
        COL_PROGRESS: PROGRESS_ASSIGNED,
        COL_REMARK: "",
    }
    defaults.update(values)
    return Record(record_id="r1", values=defaults)


def _ref(stage: str = "翻译", segment: str = "1") -> TaskRef:
    return TaskRef(show="淡岛百景", episode="07", stage=stage, segment=segment)


def test_render_claim_includes_at_and_label() -> None:
    msg = render_claim(ClaimOutcome(task=_make_record(), ref=_ref()))
    text = str(msg)
    assert "[CQ:at,qq=100]" in text
    assert "淡岛百景07 翻译 1" in text
    assert "✅" in text


def test_render_complete_normal_path_no_warning() -> None:
    outcome = CompleteOutcome(
        task=_make_record(**{COL_PROGRESS: PROGRESS_DONE}),
        ref=_ref(),
        sender_qq=100,
        original_assignee_raw="100",
        sender_was_assignee=True,
        same_stage_remaining=0,
        newly_unlocked_tasks=[],
        newly_unlocked_stages=[],
        blocking_stages=[],
    )
    text = str(render_complete(outcome))
    assert "完成了" in text
    assert "⚠️" not in text  # 正常路径无提醒


def test_render_complete_non_assignee_qq_warning() -> None:
    """D3: 发送者≠组员且组员是QQ → 提示带 @"""
    outcome = CompleteOutcome(
        task=_make_record(),
        ref=_ref(),
        sender_qq=999,
        original_assignee_raw="100",
        sender_was_assignee=False,
        same_stage_remaining=0,
        newly_unlocked_tasks=[],
        newly_unlocked_stages=[],
        blocking_stages=[],
    )
    text = str(render_complete(outcome))
    assert "⚠️" in text
    assert "你不是该任务的当前组员" in text
    assert "[CQ:at,qq=100]" in text


def test_render_complete_non_assignee_nickname_warning() -> None:
    """D3: 发送者≠组员且组员是昵称 → 文本提示，不 @"""
    outcome = CompleteOutcome(
        task=_make_record(),
        ref=_ref(),
        sender_qq=999,
        original_assignee_raw="小明同学",
        sender_was_assignee=False,
        same_stage_remaining=0,
        newly_unlocked_tasks=[],
        newly_unlocked_stages=[],
        blocking_stages=[],
    )
    text = str(render_complete(outcome))
    assert "⚠️" in text
    assert "「小明同学」承担" in text
    assert "CQ:at" not in text.replace("[CQ:at,qq=999]", "")  # 没有别的 at


def test_render_complete_with_remaining_segments() -> None:
    outcome = CompleteOutcome(
        task=_make_record(),
        ref=_ref(),
        sender_qq=100,
        original_assignee_raw="100",
        sender_was_assignee=True,
        same_stage_remaining=2,
        newly_unlocked_tasks=[],
        newly_unlocked_stages=[],
        blocking_stages=[],
    )
    text = str(render_complete(outcome))
    assert "翻译 还剩 2 个分段未完成" in text


def test_render_complete_with_newly_unlocked() -> None:
    outcome = CompleteOutcome(
        task=_make_record(),
        ref=_ref(),
        sender_qq=100,
        original_assignee_raw="100",
        sender_was_assignee=True,
        same_stage_remaining=0,
        newly_unlocked_tasks=[("校对", "0")],
        newly_unlocked_stages=["校对"],
        blocking_stages=[],
    )
    text = str(render_complete(outcome))
    assert "🎉" in text
    assert "校对 现在可以接了" in text
    assert "/接活 淡岛百景 07 校对" in text


def test_render_complete_per_segment_unlock_message() -> None:
    """D13：渲染应输出"时轴 1 现在可以接了 → /接活 ... 时轴 1"（精确到段）"""
    outcome = CompleteOutcome(
        task=_make_record(),
        ref=_ref(),
        sender_qq=100,
        original_assignee_raw="100",
        sender_was_assignee=True,
        same_stage_remaining=2,
        newly_unlocked_tasks=[("时轴", "1")],
        newly_unlocked_stages=["时轴"],
        blocking_stages=[],
    )
    text = str(render_complete(outcome))
    assert "时轴 1 现在可以接了" in text
    assert "/接活 淡岛百景 07 时轴 1" in text


def test_render_complete_unsegmented_unlock_no_segment_in_command() -> None:
    """不分段下游解锁 segment='0' 时，命令里不带段号。"""
    outcome = CompleteOutcome(
        task=_make_record(),
        ref=_ref(),
        sender_qq=100,
        original_assignee_raw="100",
        sender_was_assignee=True,
        same_stage_remaining=0,
        newly_unlocked_tasks=[("校对", "0")],
        newly_unlocked_stages=["校对"],
        blocking_stages=[],
    )
    text = str(render_complete(outcome))
    assert "校对 现在可以接了" in text
    assert "/接活 淡岛百景 07 校对" in text
    assert "校对 0" not in text  # 不带 0


def test_render_complete_multiple_segments_unlocked_together() -> None:
    """同一 stage 多个段同时解锁 → 合并成一行避免刷屏。"""
    outcome = CompleteOutcome(
        task=_make_record(),
        ref=_ref(),
        sender_qq=100,
        original_assignee_raw="100",
        sender_was_assignee=True,
        same_stage_remaining=0,
        newly_unlocked_tasks=[("后期", "1"), ("后期", "2"), ("后期", "3")],
        newly_unlocked_stages=["后期"],
        blocking_stages=[],
    )
    text = str(render_complete(outcome))
    assert "后期 1/2/3" in text
    assert text.count("🎉") == 1  # 合并成一行


def test_render_complete_with_blocking_stages_when_no_unlock() -> None:
    """同工序全完成但下游仍有别的前置未完成 → 信息性提示"""
    outcome = CompleteOutcome(
        task=_make_record(),
        ref=_ref(),
        sender_qq=100,
        original_assignee_raw="100",
        sender_was_assignee=True,
        same_stage_remaining=0,
        newly_unlocked_tasks=[],
        newly_unlocked_stages=[],
        blocking_stages=["校对"],
    )
    text = str(render_complete(outcome))
    assert "翻译 已全部完成" in text
    assert "等待其它前置完成后" in text


def test_render_complete_ats_held_downstream_holder() -> None:
    """D17/Q8：下游已被人接走 → @ 持有人「可以开始了」，不发"可接"广播。"""
    outcome = CompleteOutcome(
        task=_make_record(),
        ref=_ref(),
        sender_qq=100,
        original_assignee_raw="100",
        sender_was_assignee=True,
        same_stage_remaining=0,
        newly_unlocked_tasks=[],
        newly_unlocked_stages=[],
        blocking_stages=[],
        newly_actionable_held=[("时轴", "1", "200")],
    )
    text = str(render_complete(outcome))
    assert "[CQ:at,qq=200]" in text
    assert "时轴 1" in text
    assert "可以开始了" in text
    assert "现在可以接了" not in text  # 不发广播


def test_render_abandon_with_warning() -> None:
    outcome = AbandonOutcome(
        task=_make_record(**{COL_PROGRESS: PROGRESS_UNASSIGNED, COL_ASSIGNEE: ""}),
        ref=_ref(),
        sender_qq=999,
        original_assignee_raw="100",
        sender_was_assignee=False,
    )
    text = str(render_abandon(outcome))
    assert "放弃了" in text
    assert "⚠️" in text


def test_render_in_progress_includes_at() -> None:
    text = str(
        render_in_progress(
            InProgressOutcome(task=_make_record(), ref=_ref())
        )
    )
    assert "开始" in text
    assert "[CQ:at,qq=100]" in text


def test_render_update_lists_changes() -> None:
    outcome = UpdateOutcome(
        task=_make_record(),
        ref=_ref(),
        changed_fields={COL_REMARK: "加急"},
    )
    text = str(render_update(outcome))
    assert "已更新" in text
    assert "备注=加急" in text


# ============================================================ episode ops


def test_render_create_episode_announcement() -> None:
    inserted = [
        _make_record(**{COL_TYPE: "翻译", COL_SEGMENT: f"P{i}（...）"})
        for i in range(1, 4)
    ] + [
        _make_record(**{COL_TYPE: "校对", COL_SEGMENT: SEGMENT_NONE}),
        _make_record(**{COL_TYPE: "压制", COL_SEGMENT: SEGMENT_NONE}),
    ]
    outcome = CreateEpisodeOutcome(
        show="淡岛百景",
        episode="07",
        inserted=inserted,
        initial_unlocked_stages=["翻译"],
    )
    text = render_create_episode(outcome)
    assert "淡岛百景 第07集" in text
    assert "5 条" in text
    assert "翻译×3" in text
    assert "校对" in text  # 单条不带数量后缀
    assert "当前可接：翻译" in text


def test_render_delete_summary_lists_active_tasks() -> None:
    matched = [
        _make_record(**{COL_PROGRESS: PROGRESS_ASSIGNED, COL_ASSIGNEE: "100"}),
        _make_record(**{COL_PROGRESS: PROGRESS_UNASSIGNED, COL_ASSIGNEE: ""}),
    ]
    summary = DeleteSummary(
        show="淡岛百景",
        episode="07",
        matched=matched,
        expires_at=datetime(2026, 5, 26, 18, 0, 30),
        overwrote_previous=False,
    )
    text = str(render_delete_summary(summary))
    assert "即将删除" in text
    assert "共 2 条" in text
    assert "1 条已分配/进行中" in text
    assert "[CQ:at,qq=100]" in text  # D16：已分配组员用艾特码
    assert "确认删除" in text


def test_render_delete_summary_overwrite_note() -> None:
    summary = DeleteSummary(
        show="X",
        episode="07",
        matched=[_make_record(**{COL_PROGRESS: PROGRESS_UNASSIGNED, COL_ASSIGNEE: ""})],
        expires_at=datetime(2026, 5, 26, 18, 0, 30),
        overwrote_previous=True,
    )
    assert "已覆盖" in str(render_delete_summary(summary))


def test_render_delete_done() -> None:
    outcome = DeleteOutcome(
        show="X", episode="07", deleted=[_make_record(), _make_record()]
    )
    text = render_delete_done(outcome)
    assert "已删除" in text
    assert "2 条" in text


def test_render_archive_outcome() -> None:
    outcome = ArchiveOutcome(
        show="X",
        episode="07",
        archived=[_make_record(), _make_record()],
        skipped=[_make_record()],
    )
    text = render_archive(outcome)
    assert "归档完成" in text
    assert "2 条" in text
    assert "1 条非已完成" in text


# ============================================================ progress board


def test_render_progress_includes_all_columns_in_pipeline_order() -> None:
    records = [
        _make_record(**{
            COL_TYPE: "翻译",
            COL_SEGMENT: "1",
            COL_PROGRESS: PROGRESS_DONE,
            COL_DONE_TIME: datetime(2026, 5, 20, 18, 0),
            COL_ASSIGNEE: "100",
        }),
        _make_record(**{
            COL_TYPE: "翻译",
            COL_SEGMENT: "2",
            COL_PROGRESS: PROGRESS_UNASSIGNED,
            COL_ASSIGNEE: "",
        }),
        _make_record(**{
            COL_TYPE: "校对",
            COL_SEGMENT: SEGMENT_NONE,
            COL_PROGRESS: PROGRESS_UNASSIGNED,
            COL_ASSIGNEE: "",
        }),
    ]
    text = str(render_progress("淡岛百景", "07", records))
    assert "淡岛百景 第07集" in text
    assert "翻译" in text
    assert "校对" in text
    assert "[CQ:at,qq=100]" in text  # D16：已分配的展示真实艾特码
    assert "05-20" in text  # 完成时间月日
    # 翻译应在校对之前出现
    assert text.index("翻译") < text.index("校对")


def test_render_progress_empty_records_shows_placeholder() -> None:
    text = str(render_progress("X", "99", []))
    assert "暂无任务记录" in text


# ============================================================ list / bindings


def test_render_my_tasks_with_entries() -> None:
    tasks = [
        ("淡岛百景", _make_record(**{COL_TYPE: "翻译", COL_SEGMENT: "1"})),
        ("孤独摇滚", _make_record(**{COL_TYPE: "时轴", COL_SEGMENT: SEGMENT_NONE})),
    ]
    text = str(render_my_tasks(100, tasks))
    assert "[CQ:at,qq=100]" in text  # D16：抬头 @自己用艾特码
    assert "淡岛百景" in text
    assert "孤独摇滚" in text


def test_render_my_tasks_empty() -> None:
    assert "🎉" in str(render_my_tasks(100, []))


def test_render_available_emits_commands() -> None:
    tasks = [
        ("淡岛百景", _make_record(**{
            COL_TYPE: "翻译",
            COL_SEGMENT: "1",
            COL_ASSIGNEE: "",
            COL_PROGRESS: PROGRESS_UNASSIGNED,
        })),
    ]
    text = render_available(tasks)
    assert "/接活 淡岛百景 07 翻译 1" in text


def test_render_available_empty() -> None:
    assert "暂无可接任务" in render_available([])


def test_render_bindings_list() -> None:
    entries = [
        BindingEntry(
            alias="淡岛百景",
            group_id=111,
            file_id="F",
            sheet_id="S",
            bound_by=987,
            bound_at=datetime(2026, 5, 25, 14, 0),
        )
    ]
    text = render_bindings_list(entries, title="本群绑定的番剧")
    assert "共 1 项" in text
    assert "淡岛百景" in text
    assert "111" in text


def test_render_bindings_list_empty() -> None:
    assert "空" in render_bindings_list([], title="X")


def test_render_pipeline_view_marks_default() -> None:
    pipeline = parse_dsl("翻译 → 校对")
    text = render_pipeline_view("淡岛百景", pipeline, is_default=True)
    assert "默认流水线" in text
    assert "翻译 → 校对" in text


def test_render_pipeline_view_marks_custom() -> None:
    pipeline = parse_dsl("翻译 → 校对")
    text = render_pipeline_view("淡岛百景", pipeline, is_default=False)
    assert "自定义流水线" in text


# ============================================================ external changes (D17)


def _ext_change(kind: str, **kw) -> ExternalChange:
    base = dict(show="淡岛百景", episode="07", stage="翻译", segment="1")
    base.update(kw)
    return ExternalChange(kind=kind, **base)


def test_render_external_changes_per_line_under_threshold() -> None:
    """事件数 ≤ 阈值 → 逐条发，每条单独一条带 📝 前缀的消息。"""
    report = ExternalChangeReport(
        show="淡岛百景",
        changes=[
            _ext_change(CHANGE_CLAIMED, assignee="100"),
            _ext_change(CHANGE_COMPLETED, stage="时轴", assignee="200"),
        ],
    )
    msgs = render_external_changes(report, digest_threshold=5)
    assert len(msgs) == 2
    texts = [str(m) for m in msgs]
    assert all(t.startswith("📝") for t in texts)
    assert any("接走了" in t and "[CQ:at,qq=100]" in t for t in texts)
    assert any("完成了" in t and "[CQ:at,qq=200]" in t for t in texts)


def test_render_external_changes_digest_over_threshold() -> None:
    """事件数 > 阈值 → 合并成一条多行汇总。"""
    report = ExternalChangeReport(
        show="淡岛百景",
        changes=[_ext_change(CHANGE_ROW_ADDED, segment="0", stage=f"工序{i}") for i in range(4)],
    )
    msgs = render_external_changes(report, digest_threshold=3)
    assert len(msgs) == 1
    text = str(msgs[0])
    assert "检测到" in text and "4 处" in text
    assert text.count("新增任务") == 4


def test_render_external_changes_unlock_lines() -> None:
    report = ExternalChangeReport(
        show="淡岛百景",
        unlocked_unassigned=[("07", "时轴", "1")],
        unlocked_held=[("07", "校对", "0", "300")],
    )
    msgs = render_external_changes(report, digest_threshold=5)
    joined = "\n".join(str(m) for m in msgs)
    assert "现在可以接了" in joined
    assert "/接活 淡岛百景 07 时轴 1" in joined
    assert "[CQ:at,qq=300]" in joined
    assert "可以开始了" in joined


def test_render_external_changes_empty_returns_no_messages() -> None:
    assert render_external_changes(ExternalChangeReport(show="X")) == []
