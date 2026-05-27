"""流水线配置 + DSL 解析 + 每集快照（D10）。

DSL 语法（设计文档 3.3）：
    翻译[分段] → 时轴[分段] → 校对 → 后期 → 监制 → 压制

- "→" 串行；逗号分隔的工序并行（共同作为下一组的前置）
- "[分段]" 标注该工序按段展开（D12 起：值为 "1"/"2"/.../"N"）；不标即一条 "0"
- 容忍：箭头可写 → / -> / =>；逗号全/半角；方括号全/半角；空白随意

存储：
- data/pipelines.json          : 番剧默认流水线（每番剧一条）
- data/episode_pipelines.json  : 每集创建时的流水线快照（D10）

依赖检查约定（task_manager 使用）：
- get_episode_pipeline(show, episode) 先看快照；快照不存在则 fallback 到当前 show 流水线
- depends_on 里若有该集实际不存在的工序（如 /新建特殊 跳过了某些工序），上层应视为"满足"
  （vacuous truth），具体在 task_manager 里处理
- D13：上下游若**都标 [分段]**，依赖按"同段"判断（P1 翻译完才解锁 P1 时轴，
  与 P2/P3 翻译无关）；其它情况按"全段完成"判断。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable

from .exceptions import PipelineError
from .models import Pipeline, PipelineStage


log = logging.getLogger(__name__)


# ============================================================ DSL parse / format


_ARROW_RE = re.compile(r"\s*(?:→|->|=>)\s*")
_COMMA_RE = re.compile(r"\s*[,，]\s*")
_SEGMENT_SUFFIX_RE = re.compile(r"[\[\［]\s*分段\s*[\]\］]\s*$")
_BRACKETED_RE = re.compile(r"[\[\［].*?[\]\］]")


def parse_dsl(text: str) -> Pipeline:
    text = (text or "").strip()
    if not text:
        raise PipelineError("流水线定义为空")

    raw_groups = _ARROW_RE.split(text)
    parsed_groups: list[list[tuple[str, bool]]] = []
    seen: set[str] = set()

    for group_idx, raw_group in enumerate(raw_groups):
        raw_group = raw_group.strip()
        if not raw_group:
            raise PipelineError(
                f"第 {group_idx + 1} 组工序为空（多余的 → 箭头？）"
            )
        raw_stages = _COMMA_RE.split(raw_group)
        group: list[tuple[str, bool]] = []
        for raw_stage in raw_stages:
            raw_stage = raw_stage.strip()
            if not raw_stage:
                raise PipelineError("工序名为空（多余的逗号？）")

            segment = False
            m = _SEGMENT_SUFFIX_RE.search(raw_stage)
            if m:
                segment = True
                name = raw_stage[: m.start()].strip()
            else:
                name = raw_stage

            # 防误填：仅允许 [分段] 这一种方括号标注
            stray = _BRACKETED_RE.search(name)
            if stray:
                raise PipelineError(
                    f"工序「{raw_stage}」含有未识别的标注 {stray.group()}；"
                    f"目前只支持 [分段]"
                )
            if not name:
                raise PipelineError(f"工序名为空：{raw_stage!r}")
            if name in seen:
                raise PipelineError(f"工序「{name}」在流水线中重复出现")
            seen.add(name)
            group.append((name, segment))
        parsed_groups.append(group)

    result: Pipeline = []
    prev_names: tuple[str, ...] = ()
    for group in parsed_groups:
        for name, segment in group:
            result.append(
                PipelineStage(stage=name, segment=segment, depends_on=prev_names)
            )
        prev_names = tuple(name for name, _ in group)
    return result


def to_dsl(pipeline: Pipeline) -> str:
    """把 Pipeline 序列化回 DSL 字符串（canonical form：→ + 半角逗号 + [分段]）。"""
    if not pipeline:
        return ""
    # 按 depends_on 分层：同样的 depends_on 视为同一组（并行）
    groups: list[list[PipelineStage]] = []
    current_deps: tuple[str, ...] | None = None
    for stage in pipeline:
        if stage.depends_on != current_deps:
            groups.append([stage])
            current_deps = stage.depends_on
        else:
            groups[-1].append(stage)
    return " → ".join(
        ",".join(
            f"{s.stage}[分段]" if s.segment else s.stage for s in group
        )
        for group in groups
    )


# ============================================================ dependency queries


def downstream_of(pipeline: Pipeline, stage_name: str) -> list[PipelineStage]:
    """返回所有把 stage_name 列入 depends_on 的下游工序。"""
    return [s for s in pipeline if stage_name in s.depends_on]


def predecessors_of(pipeline: Pipeline, stage_name: str) -> tuple[str, ...]:
    for s in pipeline:
        if s.stage == stage_name:
            return s.depends_on
    return ()


def stage_names(pipeline: Pipeline) -> list[str]:
    return [s.stage for s in pipeline]


# ============================================================ store


class PipelineStore:
    def __init__(
        self,
        *,
        config_path: Path,
        snapshot_path: Path,
        default_pipeline: Pipeline,
    ) -> None:
        self._config_path = config_path
        self._snapshot_path = snapshot_path
        self._default_pipeline = list(default_pipeline)
        self._show_pipelines: dict[str, Pipeline] = {}
        self._snapshots: dict[str, dict[str, Pipeline]] = {}

    @classmethod
    def load(
        cls,
        *,
        config_path: Path,
        snapshot_path: Path,
        default_pipeline_dsl: str,
    ) -> "PipelineStore":
        default = parse_dsl(default_pipeline_dsl)
        store = cls(
            config_path=config_path,
            snapshot_path=snapshot_path,
            default_pipeline=default,
        )
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            for show, stages in (raw or {}).items():
                store._show_pipelines[show] = [
                    PipelineStage.from_dict(s) for s in stages
                ]
        if snapshot_path.exists():
            raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
            for show, eps in (raw or {}).items():
                store._snapshots[show] = {
                    ep: [PipelineStage.from_dict(s) for s in stages]
                    for ep, stages in eps.items()
                }
        return store

    # ------------------------------------------------ persistence

    def _save_configs(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            show: [s.to_dict() for s in pipe]
            for show, pipe in self._show_pipelines.items()
        }
        _atomic_write_json(self._config_path, payload)

    def _save_snapshots(self) -> None:
        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            show: {ep: [s.to_dict() for s in pipe] for ep, pipe in eps.items()}
            for show, eps in self._snapshots.items()
        }
        _atomic_write_json(self._snapshot_path, payload)

    # ------------------------------------------------ show-level pipeline

    def set_pipeline(self, show: str, dsl: str) -> Pipeline:
        """从 DSL 设置某番剧的当前流水线，落盘。"""
        pipeline = parse_dsl(dsl)
        self._show_pipelines[show] = pipeline
        self._save_configs()
        return list(pipeline)

    def remove_pipeline(self, show: str) -> bool:
        """移除某番剧的自定义流水线，恢复使用 default。返回是否真的删了。"""
        if show in self._show_pipelines:
            del self._show_pipelines[show]
            self._save_configs()
            return True
        return False

    def has_custom_pipeline(self, show: str) -> bool:
        return show in self._show_pipelines

    def get_pipeline(self, show: str) -> Pipeline:
        """取番剧当前流水线；没自定义则返回 default 的副本。"""
        return list(
            self._show_pipelines.get(show)
            or self._default_pipeline
        )

    @property
    def default_pipeline(self) -> Pipeline:
        return list(self._default_pipeline)

    # ------------------------------------------------ episode snapshots (D10)

    def snapshot_episode(
        self, show: str, episode: str, pipeline: Pipeline
    ) -> None:
        """把指定 pipeline 作为本集快照存起来（覆盖式）。"""
        self._snapshots.setdefault(show, {})[episode] = list(pipeline)
        self._save_snapshots()

    def remove_episode_snapshot(self, show: str, episode: str) -> bool:
        eps = self._snapshots.get(show)
        if eps and episode in eps:
            del eps[episode]
            if not eps:
                del self._snapshots[show]
            self._save_snapshots()
            return True
        return False

    def has_snapshot(self, show: str, episode: str) -> bool:
        return episode in self._snapshots.get(show, {})

    def get_episode_pipeline(self, show: str, episode: str) -> Pipeline:
        """D10：快照优先；没有就 fallback 当前流水线 + warning 日志。"""
        snap = self._snapshots.get(show, {}).get(episode)
        if snap is not None:
            return list(snap)
        log.warning(
            "no pipeline snapshot for %s/%s, falling back to current pipeline",
            show,
            episode,
        )
        return self.get_pipeline(show)


# ============================================================ helpers


def _atomic_write_json(path: Path, payload: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)
