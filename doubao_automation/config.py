"""配置加载与点路径访问。"""
import os
import tempfile
from pathlib import Path

import yaml

from .paths import CONFIG_DIR, ROOT


def _default_config_path():
    override = os.environ.get("DOUBAO_CONFIG_PATH", "").strip()
    if not override:
        return CONFIG_DIR / "config.yaml"
    path = Path(override).expanduser()
    if not path.is_absolute():
        raise ValueError("DOUBAO_CONFIG_PATH 必须是绝对路径")
    return path


DEFAULT_CONFIG_PATH = _default_config_path()


def load_config(path=None) -> dict:
    p = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"配置文件格式错误（顶层必须是映射）: {p}")
    return cfg


def save_config(cfg: dict, path=None):
    """Atomically persist private settings without exposing credentials."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.config-', suffix='.yaml.tmp', dir=p.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def require_doubao_accounts(cfg: dict):
    """仅在生成视频前校验，未验证登录的账号不能自动启用。"""
    accounts = get(cfg, "accounts.doubao") or []
    enabled = [a for a in accounts if a.get("enabled", True)]
    if enabled:
        return enabled
    if accounts:
        reason = "豆包账号均未启用（enabled=false），可能尚未完成登录验证"
    else:
        reason = "尚未配置豆包账号（accounts.doubao 为空）"
    raise ValueError(f"{reason}。请在工作台「账号库」添加账号或重新打开登录窗口，"
                     "完成登录验证后再运行；不要直接启用未登录的账号。")


def get(cfg: dict, dotted: str, default=None):
    """按 'a.b.c' 形式取嵌套配置；任何一级缺失或为 None 时返回 default。"""
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur
