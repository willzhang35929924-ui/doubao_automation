"""配置加载与点路径访问。"""
import os
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


def get(cfg: dict, dotted: str, default=None):
    """按 'a.b.c' 形式取嵌套配置；任何一级缺失或为 None 时返回 default。"""
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur
