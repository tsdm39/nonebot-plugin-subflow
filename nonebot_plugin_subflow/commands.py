"""所有面向 QQ 群的命令处理器。

约定：
- 每个命令一个 Matcher，写操作必走 admin 权限
- 总群对写命令一律拒绝（D9），允许查询
- 业务异常 → 友好中文回复；未预期异常 → 日志 + 「内部错误」回复
- 命令的解析尽量宽容（D6 归一化在 task_manager 内做，命令层不再重复）
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from nonebot import on_command, on_fullmatch
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import (
    GROUP_ADMIN,
    GROUP_OWNER,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.exception import MatcherException
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.permission import Permission

from . import deps, render
from .exceptions import (
    BindingError,
    PipelineError,
    StorageError,
    TokenExpiredError,
)
from .task_manager import TaskError


log = logging.getLogger(__name__)


# ============================================================ permissions (lazy, read deps.config)


async def _is_subflow_super_admin(bot: Bot, event: Event) -> bool:
    cfg = deps.config
    if cfg is None:
        return False
    try:
        return int(event.get_user_id()) in cfg.subflow_admin_qq_list
    except (ValueError, NotImplementedError):
        return False


SUBFLOW_SUPER_ADMIN = Permission(_is_subflow_super_admin)
SUBFLOW_ADMIN = GROUP_OWNER | GROUP_ADMIN | SUBFLOW_SUPER_ADMIN


# ============================================================ helpers


async def _reject_if_main_group(matcher: Matcher, event: GroupMessageEvent) -> None:
    """D9：总群拒绝写操作。"""
    if deps.require_bindings().is_main_group(event.group_id):
        await matcher.finish("⚠️ 写操作请到对应工作群执行，总群仅支持查询")


def _split_args(args: Message) -> list[str]:
    return args.extract_plain_text().strip().split()


def _parse_segment_count(token: str | None) -> int:
    """D12：`/新建集 X 7 3` 里的 "3" → 3；省略则默认 1。"""
    if token is None or token == "":
        return 1
    try:
        n = int(token)
    except ValueError:
        raise ValueError(f"分段数必须是整数：「{token}」")
    if n < 1:
        raise ValueError(f"分段数必须 ≥ 1（当前 {n}）")
    return n


def _parse_kv(token: str) -> tuple[str, str]:
    """`备注=加急` → (备注, 加急)。"""
    if "=" not in token:
        raise ValueError(f"字段参数格式错误：{token}（应为 字段=值）")
    k, v = token.split("=", 1)
    return k.strip(), v.strip()


async def _send_user_error(matcher: Matcher, exc: Exception) -> None:
    """已知异常 → 友好中文；未知 → 日志 + 通用提示。

    注意：MatcherException（finish/reject/pause）必须重新抛出，否则会被吞掉
    导致 finish 不生效。
    """
    if isinstance(exc, MatcherException):
        raise exc
    if isinstance(exc, TokenExpiredError):
        await matcher.send(
            "❌ access_token 已失效，请管理员到腾讯文档开放平台后台重新生成并更新 .env"
        )
        return
    if isinstance(exc, (TaskError, BindingError, PipelineError)):
        await matcher.send(f"❌ {exc}")
        return
    if isinstance(exc, StorageError):
        await matcher.send(f"❌ 远端错误：{exc}")
        return
    log.exception("unhandled error in subflow command")
    await matcher.send("❌ 内部错误，请查看日志")


def _maybe_to_real_file_id(value: str) -> str:
    """支持用户传 encodedID 或真实 fileID。真实形如 `300000000$xxx`。"""
    return value


async def _resolve_real_file_id(value: str) -> str:
    """encodedID 自动换；已是真 file_id 则原样返回。"""
    if "$" in value:
        return value
    storage = deps.require_storage()
    return await storage.convert_encoded_id(value)


# ============================================================ /绑定 /绑定id /解绑 /绑定列表


bind_matcher = on_command(
    "绑定", priority=10, block=True, permission=SUBFLOW_ADMIN
)


@bind_matcher.handle()
async def _handle_bind(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(bind_matcher, event)
        cfg = deps.config
        if cfg is None or not cfg.tencent_doc_default_file_id:
            await bind_matcher.finish(
                "❌ 未配置 TENCENT_DOC_DEFAULT_FILE_ID，无法按名查找子表；"
                "请用 /绑定id <fileID> <sheetID> <别名> 直接绑定"
            )
        tokens = _split_args(args)
        if len(tokens) != 1:
            await bind_matcher.finish("用法：/绑定 <番剧名>")
        show = tokens[0]
        bindings = deps.require_bindings()
        storage = deps.require_storage()
        file_id = await _resolve_real_file_id(cfg.tencent_doc_default_file_id)
        # 在默认文档中查找名为 {番剧名}_工作表 的子表
        # storage.get_sheets 尚未实现 → 暂时不支持，提示用 /绑定id
        await bind_matcher.finish(
            "⚠️ /绑定 暂未实现（依赖 storage.get_sheets，未在 M1 验证）；"
            "请改用 /绑定id <fileID> <sheetID> <别名>"
        )
    except Exception as exc:
        await _send_user_error(bind_matcher, exc)


bind_id_matcher = on_command(
    "绑定id", priority=10, block=True, permission=SUBFLOW_ADMIN
)


@bind_id_matcher.handle()
async def _handle_bind_id(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(bind_id_matcher, event)
        tokens = _split_args(args)
        if len(tokens) != 3:
            await bind_id_matcher.finish(
                "用法：/绑定id <fileID或URL编码ID> <sheetID> <别名>"
            )
        raw_file_id, sheet_id, alias = tokens
        file_id = await _resolve_real_file_id(raw_file_id)
        bindings = deps.require_bindings()
        entry = bindings.bind(
            group_id=event.group_id,
            alias=alias,
            file_id=file_id,
            sheet_id=sheet_id,
            bound_by=event.user_id,
        )
        # 启动后绑定的子表也要加载到缓存
        cache = deps.require_cache()
        n = await cache.add_sheet(file_id, sheet_id)
        await bind_id_matcher.send(
            f"✅ 已绑定「{alias}」→ {file_id}/{sheet_id}（加载 {n} 条记录）"
        )
    except Exception as exc:
        await _send_user_error(bind_id_matcher, exc)


unbind_matcher = on_command(
    "解绑", priority=10, block=True, permission=SUBFLOW_ADMIN
)


@unbind_matcher.handle()
async def _handle_unbind(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(unbind_matcher, event)
        tokens = _split_args(args)
        if len(tokens) != 1:
            await unbind_matcher.finish("用法：/解绑 <番剧名>")
        alias = tokens[0]
        bindings = deps.require_bindings()
        entry = bindings.unbind(group_id=event.group_id, alias=alias)
        cache = deps.require_cache()
        cache.remove_sheet(entry.file_id, entry.sheet_id)
        await unbind_matcher.send(f"✅ 已解绑「{alias}」")
    except Exception as exc:
        await _send_user_error(unbind_matcher, exc)


bindings_list_matcher = on_command("绑定列表", priority=10, block=True)


@bindings_list_matcher.handle()
async def _handle_bindings_list(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        tokens = _split_args(args)
        bindings = deps.require_bindings()
        # 子命令 全部：要求 super admin
        if tokens and tokens[0] == "全部":
            if not await _is_subflow_super_admin(None, event):  # type: ignore[arg-type]
                await bindings_list_matcher.finish(
                    "⚠️ /绑定列表 全部 仅 SUBFLOW_ADMIN_QQ_LIST 内的超管可用"
                )
            text = render.render_bindings_list(
                bindings.list_all(), title="全部群的绑定"
            )
        else:
            if bindings.is_main_group(event.group_id):
                text = render.render_bindings_list(
                    bindings.list_all(), title="所有番剧（总群视图）"
                )
            else:
                text = render.render_bindings_list(
                    bindings.get_for_group(event.group_id),
                    title="本群绑定的番剧",
                )
        await bindings_list_matcher.send(text)
    except Exception as exc:
        await _send_user_error(bindings_list_matcher, exc)


# ============================================================ /设置流水线 /查看流水线


set_pipeline_matcher = on_command(
    "设置流水线", priority=10, block=True, permission=SUBFLOW_ADMIN
)


@set_pipeline_matcher.handle()
async def _handle_set_pipeline(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(set_pipeline_matcher, event)
        raw = args.extract_plain_text().strip()
        if " " not in raw:
            await set_pipeline_matcher.finish(
                "用法：/设置流水线 <番剧名> 翻译[分段] → 时轴[分段] → 校对 → ..."
            )
        show, dsl = raw.split(" ", 1)
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=show)
        pipelines = deps.require_pipelines()
        new_pipeline = pipelines.set_pipeline(entry.alias, dsl)
        await set_pipeline_matcher.send(
            render.render_pipeline_view(
                entry.alias, new_pipeline, is_default=False
            )
        )
    except Exception as exc:
        await _send_user_error(set_pipeline_matcher, exc)


view_pipeline_matcher = on_command("查看流水线", priority=10, block=True)


@view_pipeline_matcher.handle()
async def _handle_view_pipeline(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        tokens = _split_args(args)
        hint = tokens[0] if tokens else None
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=hint)
        pipelines = deps.require_pipelines()
        pipeline = pipelines.get_pipeline(entry.alias)
        is_default = not pipelines.has_custom_pipeline(entry.alias)
        await view_pipeline_matcher.send(
            render.render_pipeline_view(entry.alias, pipeline, is_default)
        )
    except Exception as exc:
        await _send_user_error(view_pipeline_matcher, exc)


# ============================================================ /新建集 /新建特殊


create_episode_matcher = on_command(
    "新建集", priority=10, block=True, permission=SUBFLOW_ADMIN
)


@create_episode_matcher.handle()
async def _handle_create_episode(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(create_episode_matcher, event)
        tokens = _split_args(args)
        if len(tokens) < 1:
            await create_episode_matcher.finish(
                "用法：/新建集 <番剧名> <集数> [分段数=1]"
            )
        # D12 语法：<番剧名> <集数> [分段数]；单绑定时番剧名可省
        bindings = deps.require_bindings()
        first, *rest = tokens
        try:
            entry = bindings.resolve(group_id=event.group_id, hint=first)
            episode = rest[0] if rest else None
            seg_count_token = rest[1] if len(rest) >= 2 else None
        except Exception:
            entry = bindings.resolve(group_id=event.group_id, hint=None)
            episode = first
            seg_count_token = rest[0] if rest else None
        if not episode:
            await create_episode_matcher.finish(
                "用法：/新建集 <番剧名> <集数> [分段数=1]"
            )
        segment_count = _parse_segment_count(seg_count_token)
        tm = deps.require_task_manager()
        outcome = await tm.create_episode(entry.alias, episode, segment_count)
        await create_episode_matcher.send(render.render_create_episode(outcome))
    except Exception as exc:
        await _send_user_error(create_episode_matcher, exc)


create_special_matcher = on_command(
    "新建特殊", priority=10, block=True, permission=SUBFLOW_ADMIN
)


@create_special_matcher.handle()
async def _handle_create_special(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(create_special_matcher, event)
        tokens = _split_args(args)
        if len(tokens) < 3:
            await create_special_matcher.finish(
                "用法：/新建特殊 <番剧名> <集数标识> <类型1> [类型2] ..."
            )
        show, episode, *stages = tokens
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=show)
        tm = deps.require_task_manager()
        outcome = await tm.create_special(entry.alias, episode, stages)
        await create_special_matcher.send(render.render_create_episode(outcome))
    except Exception as exc:
        await _send_user_error(create_special_matcher, exc)


# ============================================================ /删除任务 + 确认删除


delete_task_matcher = on_command(
    "删除任务", priority=10, block=True, permission=SUBFLOW_ADMIN
)


@delete_task_matcher.handle()
async def _handle_delete_task(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(delete_task_matcher, event)
        tokens = _split_args(args)
        if len(tokens) < 2:
            await delete_task_matcher.finish(
                "用法：/删除任务 <番剧名> <集数> [类型] [分段]"
            )
        show, episode, *rest = tokens
        stage = rest[0] if len(rest) >= 1 else None
        segment = rest[1] if len(rest) >= 2 else None
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=show)
        tm = deps.require_task_manager()
        summary = tm.prepare_delete(
            group_id=event.group_id,
            user_qq=event.user_id,
            show=entry.alias,
            episode=episode,
            stage=stage,
            segment=segment,
        )
        await delete_task_matcher.send(render.render_delete_summary(summary))
    except Exception as exc:
        await _send_user_error(delete_task_matcher, exc)


confirm_delete_matcher = on_fullmatch("确认删除", priority=5, block=True)


@confirm_delete_matcher.handle()
async def _handle_confirm_delete(event: GroupMessageEvent) -> None:
    try:
        tm = deps.require_task_manager()
        if not tm.has_pending(group_id=event.group_id, user_qq=event.user_id):
            # 没有 pending 不要喧宾夺主 —— 静默放过（让其他 matcher 有机会处理）
            return
        outcome = await tm.confirm_pending(
            group_id=event.group_id, user_qq=event.user_id
        )
        await confirm_delete_matcher.send(render.render_delete_done(outcome))
    except Exception as exc:
        await _send_user_error(confirm_delete_matcher, exc)


# ============================================================ /修改任务


update_task_matcher = on_command(
    "修改任务", priority=10, block=True, permission=SUBFLOW_ADMIN
)


@update_task_matcher.handle()
async def _handle_update_task(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(update_task_matcher, event)
        tokens = _split_args(args)
        # 至少 4 个 token：show episode stage [segment] kv，kv 必含 =
        if len(tokens) < 4:
            await update_task_matcher.finish(
                "用法：/修改任务 <番剧名> <集数> <类型> [分段] <字段>=<值>"
            )
        # 最后一个 token 是 kv
        kv_token = tokens[-1]
        if "=" not in kv_token:
            await update_task_matcher.finish(
                "最后一个参数必须是 字段=值 形式，如 备注=加急"
            )
        key, value = _parse_kv(kv_token)
        head = tokens[:-1]
        show, episode, stage = head[0], head[1], head[2]
        segment = head[3] if len(head) >= 4 else None
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=show)
        tm = deps.require_task_manager()
        outcome = await tm.update_task(
            entry.alias, episode, stage, segment, {key: value}
        )
        await update_task_matcher.send(render.render_update(outcome))
    except Exception as exc:
        await _send_user_error(update_task_matcher, exc)


# ============================================================ /归档


archive_matcher = on_command(
    "归档", priority=10, block=True, permission=SUBFLOW_ADMIN
)


@archive_matcher.handle()
async def _handle_archive(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(archive_matcher, event)
        tokens = _split_args(args)
        if len(tokens) != 2:
            await archive_matcher.finish("用法：/归档 <番剧名> <集数>")
        show, episode = tokens
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=show)
        tm = deps.require_task_manager()
        outcome = await tm.archive_episode(entry.alias, episode)
        await archive_matcher.send(render.render_archive(outcome))
    except Exception as exc:
        await _send_user_error(archive_matcher, exc)


# ============================================================ /接活 /完成 /放弃 /进行中


def _parse_task_args(args: Message, *, expect_segment: bool) -> tuple[str, str, str, str | None]:
    """tokens: <番剧名> <集数> <类型> [分段]。返回 (show, ep, stage, segment)。"""
    tokens = _split_args(args)
    if len(tokens) < 3:
        raise ValueError("用法：<番剧名> <集数> <类型> [分段]")
    show, episode, stage = tokens[0], tokens[1], tokens[2]
    segment = tokens[3] if len(tokens) >= 4 else None
    return show, episode, stage, segment


claim_matcher = on_command("接活", priority=10, block=True)


@claim_matcher.handle()
async def _handle_claim(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(claim_matcher, event)
        show, ep, stage, segment = _parse_task_args(args, expect_segment=False)
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=show)
        tm = deps.require_task_manager()
        outcome = await tm.claim_task(
            entry.alias, ep, stage, segment, user_qq=event.user_id
        )
        await claim_matcher.send(render.render_claim(outcome))
    except Exception as exc:
        await _send_user_error(claim_matcher, exc)


complete_matcher = on_command("完成", priority=10, block=True)


@complete_matcher.handle()
async def _handle_complete(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(complete_matcher, event)
        show, ep, stage, segment = _parse_task_args(args, expect_segment=False)
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=show)
        tm = deps.require_task_manager()
        outcome = await tm.complete_task(
            entry.alias, ep, stage, segment, user_qq=event.user_id
        )
        await complete_matcher.send(render.render_complete(outcome))
    except Exception as exc:
        await _send_user_error(complete_matcher, exc)


abandon_matcher = on_command("放弃", priority=10, block=True)


@abandon_matcher.handle()
async def _handle_abandon(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(abandon_matcher, event)
        show, ep, stage, segment = _parse_task_args(args, expect_segment=False)
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=show)
        tm = deps.require_task_manager()
        outcome = await tm.abandon_task(
            entry.alias, ep, stage, segment, user_qq=event.user_id
        )
        await abandon_matcher.send(render.render_abandon(outcome))
    except Exception as exc:
        await _send_user_error(abandon_matcher, exc)


in_progress_matcher = on_command("进行中", priority=10, block=True)


@in_progress_matcher.handle()
async def _handle_in_progress(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        await _reject_if_main_group(in_progress_matcher, event)
        show, ep, stage, segment = _parse_task_args(args, expect_segment=False)
        bindings = deps.require_bindings()
        entry = bindings.resolve(group_id=event.group_id, hint=show)
        tm = deps.require_task_manager()
        outcome = await tm.set_in_progress(
            entry.alias, ep, stage, segment, user_qq=event.user_id
        )
        await in_progress_matcher.send(render.render_in_progress(outcome))
    except Exception as exc:
        await _send_user_error(in_progress_matcher, exc)


# ============================================================ /进度 /我的任务 /待接（查询，工作群+总群均可）


progress_matcher = on_command("进度", priority=10, block=True)


@progress_matcher.handle()
async def _handle_progress(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        tokens = _split_args(args)
        if not tokens:
            await progress_matcher.finish("用法：/进度 <番剧名> [集数]")
        bindings = deps.require_bindings()
        # 总群必须给番剧名；工作群可省（推断）
        if bindings.is_main_group(event.group_id):
            if len(tokens) < 1:
                await progress_matcher.finish("总群用法：/进度 <番剧名> [集数]")
            show, *rest = tokens
            entry = bindings.resolve(group_id=event.group_id, hint=show)
            episode = rest[0] if rest else None
        else:
            # 工作群：第一个 token 可能是 show（多绑定时必需）或集数（单绑定省略）
            try:
                entry = bindings.resolve(group_id=event.group_id, hint=tokens[0])
                episode = tokens[1] if len(tokens) >= 2 else None
            except Exception:
                entry = bindings.resolve(group_id=event.group_id, hint=None)
                episode = tokens[0]
        tm = deps.require_task_manager()
        if episode is None:
            await progress_matcher.finish(
                f"{entry.alias} 当前共绑定 — 请补集数：/进度 {entry.alias} <集数>"
            )
        records = tm.list_episode(entry.alias, episode)
        await progress_matcher.send(
            render.render_progress(entry.alias, episode, records)
        )
    except Exception as exc:
        await _send_user_error(progress_matcher, exc)


my_tasks_matcher = on_command("我的任务", priority=10, block=True)


@my_tasks_matcher.handle()
async def _handle_my_tasks(event: GroupMessageEvent) -> None:
    try:
        bindings = deps.require_bindings()
        tm = deps.require_task_manager()
        if bindings.is_main_group(event.group_id):
            tasks = tm.list_my_tasks(event.user_id)
        else:
            aliases = bindings.aliases_for_group(event.group_id)
            tasks = tm.list_my_tasks(event.user_id, show_filter=aliases)
        await my_tasks_matcher.send(render.render_my_tasks(event.user_id, tasks))
    except Exception as exc:
        await _send_user_error(my_tasks_matcher, exc)


available_matcher = on_command("待接", priority=10, block=True)


@available_matcher.handle()
async def _handle_available(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    try:
        tokens = _split_args(args)
        bindings = deps.require_bindings()
        tm = deps.require_task_manager()
        if tokens:
            # 指定番剧
            entry = bindings.resolve(group_id=event.group_id, hint=tokens[0])
            tasks = tm.list_available(show_filter=[entry.alias])
        elif bindings.is_main_group(event.group_id):
            tasks = tm.list_available()
        else:
            aliases = bindings.aliases_for_group(event.group_id)
            tasks = tm.list_available(show_filter=aliases)
        await available_matcher.send(render.render_available(tasks))
    except Exception as exc:
        await _send_user_error(available_matcher, exc)
