"""Private, encrypted settings for optional remote caption recognition."""

import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .paths import CONFIG_DIR


_SETTINGS_NAME = "caption_settings.json"
_KEY_NAME = "caption_settings.key"
_LOCK = threading.RLock()
_PROMPT_KEYS = ("image_prompt", "product_video_prompt", "reference_video_prompt")


class CaptionSettingsError(ValueError):
    """A safe, user-facing caption settings error."""


def _crypto():
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        raise CaptionSettingsError("云端识别需要加密依赖，请在项目环境安装 requirements.txt") from None
    return Fernet, InvalidToken


def _defaults():
    from .caption_runtime import DEFAULT_CAPTION_PROMPTS

    if not isinstance(DEFAULT_CAPTION_PROMPTS, dict) or not all(
        isinstance(DEFAULT_CAPTION_PROMPTS.get(name), str) and DEFAULT_CAPTION_PROMPTS[name]
        for name in _PROMPT_KEYS
    ):
        raise CaptionSettingsError("识别模型的默认提示词配置无效")
    return {name: DEFAULT_CAPTION_PROMPTS[name] for name in _PROMPT_KEYS}


def _validate_limits(settings):
    from .caption_runtime import MAX_CAPTION_PROMPT_LENGTH

    if len(settings["model"].strip()) > 256:
        raise CaptionSettingsError("识别模型名称最多 256 字")
    if any(len(settings[name].strip()) > MAX_CAPTION_PROMPT_LENGTH for name in _PROMPT_KEYS):
        raise CaptionSettingsError(f"识别提示词最多 {MAX_CAPTION_PROMPT_LENGTH} 字")
    if any(not 33 <= ord(char) <= 126 for char in settings["api_key"].strip()):
        raise CaptionSettingsError("API Key 只能包含无空格的可打印 ASCII 字符")


def _paths():
    return CONFIG_DIR / _SETTINGS_NAME, CONFIG_DIR / _KEY_NAME


def _ensure_config_dir():
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


def _read_key(path: Path):
    Fernet, _ = _crypto()

    try:
        if path.is_symlink() or (os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise OSError
        key = path.read_bytes()
        return Fernet(key)
    except (OSError, ValueError) as exc:
        raise CaptionSettingsError("识别模型密钥文件缺失或损坏；请从本机备份恢复") from exc


def _create_key(path: Path):
    Fernet, _ = _crypto()

    _ensure_config_dir()
    if path.exists() or path.is_symlink():
        return _read_key(path)
    key = Fernet.generate_key()
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _read_key(path)
    except OSError:
        raise CaptionSettingsError("识别模型密钥文件无法保存") from None
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise CaptionSettingsError("识别模型密钥文件无法保存") from None
    return Fernet(key)


def _load():
    settings_path, key_path = _paths()
    defaults = _defaults()
    if not settings_path.exists() and not settings_path.is_symlink():
        return dict(base_url="", model="", api_key="", **defaults)
    try:
        if settings_path.is_symlink():
            raise ValueError
        stored = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(stored, dict) or stored.get("version") != 1:
            raise ValueError
        for name in ("base_url", "model", *_PROMPT_KEYS):
            if not isinstance(stored.get(name), str):
                raise ValueError
        if any(not stored[name].strip() for name in _PROMPT_KEYS):
            raise ValueError
        base_url = _normalize_url(stored["base_url"])
        token = stored.get("api_key_ciphertext", "")
        if not isinstance(token, str) or ("api_key" in stored):
            raise ValueError
        if token:
            _, InvalidToken = _crypto()
            try:
                binding = json.loads(_read_key(key_path).decrypt(token.encode("ascii")).decode("utf-8"))
            except InvalidToken:
                raise ValueError from None
            if (not isinstance(binding, dict) or binding.get("base_url") != base_url
                    or not isinstance(binding.get("api_key"), str)):
                raise ValueError
            api_key = binding["api_key"]
        else:
            api_key = ""
        if token and not api_key:
            raise ValueError
        settings = {name: stored[name] for name in ("model", *_PROMPT_KEYS)} | {
            "base_url": base_url, "api_key": api_key,
        }
        _validate_limits(settings)
        return settings
    except CaptionSettingsError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise CaptionSettingsError("识别模型设置损坏或无法解密；请从本机备份恢复") from exc


def _public(settings):
    key_present = bool(settings["api_key"])
    return {name: settings[name] for name in ("base_url", "model", *_PROMPT_KEYS)} | {
        "api_key_configured": key_present,
        "api_key_mask": "****" if key_present else "",
        "mode": "cloud" if settings["base_url"] and settings["model"] and key_present else "local",
    }


def read_caption_settings():
    """Return the private settings. Callers must never log or JSON-serialize api_key."""
    with _LOCK:
        return _load()


def public_caption_settings():
    with _LOCK:
        return _public(_load())


def _normalize_url(value):
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise CaptionSettingsError("识别模型地址必须是有效的 HTTPS URL") from None
    if (parsed.scheme.lower() != "https" or not hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or "?" in value or "#" in value or "\\" in value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or not parsed.netloc or parsed.path.startswith("//") or port == 0):
        raise CaptionSettingsError("识别模型地址只能使用不含凭据、查询和片段的 HTTPS URL")
    normalized = "https://" + parsed.netloc.lower() + parsed.path.rstrip("/")
    if len(normalized) > 2048:
        raise CaptionSettingsError("识别模型地址最多 2048 字")
    return normalized


def _atomic_write(path: Path, content: bytes):
    _ensure_config_dir()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".caption-settings-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise CaptionSettingsError("识别模型设置无法保存，请检查本机配置目录权限") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def save_caption_settings(payload):
    """Merge a UI update and atomically persist its encrypted API key."""
    if not isinstance(payload, dict):
        raise CaptionSettingsError("识别模型设置必须是 JSON 对象")
    allowed = {"base_url", "model", "api_key", "clear_api_key", *_PROMPT_KEYS}
    if set(payload) - allowed:
        raise CaptionSettingsError("识别模型设置包含不支持的字段")
    if "clear_api_key" in payload and type(payload["clear_api_key"]) is not bool:
        raise CaptionSettingsError("清除密钥选项必须是布尔值")
    for name in allowed - {"clear_api_key"}:
        if name in payload and not isinstance(payload[name], str):
            raise CaptionSettingsError("识别模型设置字段必须是文本")
    with _LOCK:
        previous = _load()
        defaults = _defaults()
        updated = dict(previous)
        for name in ("base_url", "model"):
            if name in payload:
                updated[name] = payload[name].strip()
        updated["base_url"] = _normalize_url(updated["base_url"])
        for name in _PROMPT_KEYS:
            if name in payload:
                updated[name] = payload[name].strip() or defaults[name]
        new_key = payload.get("api_key", "").strip()
        if payload.get("clear_api_key"):
            updated["api_key"] = ""
        elif new_key:
            updated["api_key"] = new_key
        elif updated["base_url"] != previous["base_url"]:
            updated["api_key"] = ""
        try:
            for value in updated.values():
                value.encode("utf-8")
        except UnicodeError:
            raise CaptionSettingsError("识别模型设置包含无效文本字符") from None
        _validate_limits(updated)
        settings_path, key_path = _paths()
        token = ""
        if updated["api_key"]:
            fernet = _create_key(key_path)
            binding = json.dumps({"base_url": updated["base_url"], "api_key": updated["api_key"]},
                                 ensure_ascii=False, sort_keys=True).encode("utf-8")
            token = fernet.encrypt(binding).decode("ascii")
        stored = {name: updated[name] for name in ("base_url", "model", *_PROMPT_KEYS)}
        stored.update(version=1, api_key_ciphertext=token)
        _atomic_write(settings_path, (json.dumps(stored, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        return _public(updated)
