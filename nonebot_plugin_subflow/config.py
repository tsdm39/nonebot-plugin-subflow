"""配置层。

两种用法：
1. 在 NoneBot2 里跑：用 `Config` 模型 + `nonebot.get_plugin_config(Config)` 读取
2. 脱离 NoneBot 跑（如集成测试 spike/.env.spike）：用 `load_env_file` + `load_tencent_creds`
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


@dataclass
class TencentCreds:
    client_id: str
    open_id: str
    access_token: str


def load_env_file(path: Path) -> dict[str, str]:
    """读 KEY=VALUE 格式的 .env，忽略空行/注释。"""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def load_tencent_creds(env_file: Path | None = None) -> TencentCreds:
    """优先 process env，再读 env_file。"""
    file_env = load_env_file(env_file) if env_file else {}

    def _get(key: str) -> str:
        v = os.environ.get(key) or file_env.get(key)
        if not v:
            raise RuntimeError(f"missing required config: {key}")
        return v

    return TencentCreds(
        client_id=_get("TENCENT_DOC_CLIENT_ID"),
        open_id=_get("TENCENT_DOC_OPEN_ID"),
        access_token=_get("TENCENT_DOC_ACCESS_TOKEN"),
    )


class Config(BaseModel):
    """NoneBot 插件配置。所有字段从 .env / 环境变量读取。"""

    # ──── 腾讯文档 API ────
    tencent_doc_client_id: str
    tencent_doc_open_id: str
    tencent_doc_access_token: str
    tencent_doc_default_file_id: str = ""  # 默认文档 ID（未来按名绑定时查找子表用），M4 暂不强制

    # ──── 群配置 ────
    subflow_main_group_id: int | None = None
    subflow_admin_qq_list: list[int] = Field(default_factory=list)

    # ──── 业务参数 ────
    subflow_max_tasks_per_user: int = 5
    subflow_sync_interval: int = 30  # 分钟
    subflow_confirm_timeout: int = 30  # 秒
    subflow_token_warn_days: int = 7  # 天
    subflow_data_dir: str = "./data"
    # D17：外部表格变更检测与提醒
    subflow_notify_external_changes: bool = True  # 同步时检测人工直改并播报到工作群
    subflow_external_change_digest_threshold: int = 5  # 每群本轮变更 > 此数则汇总成一条
    subflow_default_pipeline: str = (
        "翻译[分段] → 时轴[分段] → 校对 → 后期 → 监制 → 压制"
    )

    @model_validator(mode="before")
    @classmethod
    def _empty_str_to_none(cls, data: Any) -> Any:
        """.env 中 KEY= 得空字符串；对非 str 类型字段转为 None 以触发 Pydantic 默认值回退。"""
        if not isinstance(data, dict):
            return data
        _STR_FIELD_NAMES = {
            "tencent_doc_client_id",
            "tencent_doc_open_id",
            "tencent_doc_access_token",
            "tencent_doc_default_file_id",
            "subflow_data_dir",
            "subflow_default_pipeline",
        }
        return {
            k: (
                None
                if k not in _STR_FIELD_NAMES and isinstance(v, str) and v == ""
                else v
            )
            for k, v in data.items()
        }
