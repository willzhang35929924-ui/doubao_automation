"""项目源码与平台用户数据的路径管理。"""
import os
import sys
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent


def _data_root():
    override = os.environ.get("DOUBAO_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("DOUBAO_DATA_DIR 必须是绝对路径")
        return path
    if sys.platform == "win32":
        return ROOT
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DoubaoAutomation"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    if not base.is_absolute():
        raise ValueError("XDG_DATA_HOME 必须是绝对路径")
    return base / "DoubaoAutomation"


DATA_ROOT = _data_root()
CONFIG_DIR = DATA_ROOT / "config"
PROFILES = DATA_ROOT / "profiles"
WORKSPACE = DATA_ROOT / "workspace"
FEISHU_DIR = WORKSPACE / "feishu"
PRODUCTS_DIR = FEISHU_DIR / "products"
SOURCES_DIR = FEISHU_DIR / "sources"
SCRIPTS_DIR = WORKSPACE / "scripts"
VIDEOS_DIR = WORKSPACE / "videos"
FRAMES_DIR = WORKSPACE / "frames"
STATE_DIR = WORKSPACE / "state"
LOGS_DIR = DATA_ROOT / "logs"
TEMP_DIR = DATA_ROOT / ".tmp"

ALL_DIRS = [PROFILES, WORKSPACE, FEISHU_DIR, PRODUCTS_DIR, SOURCES_DIR,
            SCRIPTS_DIR, VIDEOS_DIR, FRAMES_DIR, STATE_DIR, LOGS_DIR, TEMP_DIR]


def ensure_dirs() -> None:
    for directory in ALL_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def use_workspace_temp() -> None:
    """为子进程使用当前平台用户数据目录中的临时目录。"""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    for var in ("TEMP", "TMP", "TMPDIR"):
        os.environ[var] = str(TEMP_DIR)


def resolve(path_like) -> Path:
    """运行数据相对数据目录，其他相对路径相对源码目录。"""
    value = str(path_like)
    if sys.platform != "win32" and PureWindowsPath(value).is_absolute():
        raise ValueError("请为当前平台配置本机路径，不能使用 Windows 绝对路径")
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] in {"profiles", "workspace", "logs", ".tmp", "config"}:
        result = (DATA_ROOT / path).resolve()
        if result != DATA_ROOT.resolve() and DATA_ROOT.resolve() not in result.parents:
            raise ValueError("相对数据路径不能逃逸用户数据目录")
        return result
    return ROOT / path


def safe_filename(name: str, fallback: str = "file") -> str:
    """去掉 Windows 不允许的字符。"""
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad else ch for ch in str(name)).strip().strip(".")
    return cleaned or fallback
