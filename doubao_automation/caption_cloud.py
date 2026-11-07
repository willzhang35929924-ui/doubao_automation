"""Send selected media or extracted page evidence to an OpenAI-compatible API."""

import base64
import json
import ssl
import tempfile
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .caption_runtime import (
    CaptionRuntimeError,
    MAX_PAGE_CAPTION_LENGTH,
    _custom_instructions,
    _media_paths,
    _page_caption_messages,
    _page_description,
    _video_frames,
    _video_tools,
    _visual_prompt,
)


_MAX_IMAGE_BYTES = 8 * 1024**2
_MAX_TOTAL_IMAGE_BYTES = 20 * 1024**2
_MAX_RESPONSE_BYTES = 1024**2
_TIMEOUT = 60


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _chat_endpoint(base_url: str) -> str:
    try:
        value = base_url.strip()
        parsed = urlsplit(value)
        if (
            not value or len(value) > 2048
            or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port == 0 or any(c in value for c in "?#\\")
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError
    except (AttributeError, ValueError):
        raise CaptionRuntimeError("云端 Base URL 必须是没有账号信息、查询参数或片段的 HTTPS 地址。") from None
    path = parsed.path.rstrip("/") or "/v1"
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return parsed._replace(path=path).geturl()


def _image_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    raise CaptionRuntimeError("云端识别支持 JPEG、PNG、GIF 和 WebP 图片。")


def _image_content(paths: list[Path]) -> list[dict]:
    content = []
    total = 0
    for index, path in enumerate(paths, start=1):
        limit = min(_MAX_IMAGE_BYTES, _MAX_TOTAL_IMAGE_BYTES - total)
        try:
            with path.open("rb") as source:
                data = source.read(limit + 1)
        except OSError:
            raise CaptionRuntimeError(f"无法读取第 {index} 张云端识别图片。") from None
        if len(data) > limit:
            if limit < _MAX_IMAGE_BYTES:
                raise CaptionRuntimeError("云端识别图片合计最多 20 MiB。")
            raise CaptionRuntimeError(f"第 {index} 张云端识别图片超过 8 MiB。")
        total += len(data)
        mime = _image_type(data)
        encoded = base64.b64encode(data).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{encoded}"}})
    return content


def _status_error(status: int) -> CaptionRuntimeError:
    if 300 <= status < 400:
        return CaptionRuntimeError("云端接口返回重定向，请配置最终的 HTTPS 地址。")
    if status in (401, 403):
        return CaptionRuntimeError(f"云端认证或访问权限不足（HTTP {status}）。")
    if status == 404:
        return CaptionRuntimeError("云端接口或模型不存在（HTTP 404）。")
    if status == 429:
        return CaptionRuntimeError("云端请求受频率或额度限制（HTTP 429）。")
    if status in (400, 413, 415, 422):
        return CaptionRuntimeError(f"云端服务拒绝识别请求，请核对模型和输入格式（HTTP {status}）。")
    return CaptionRuntimeError(f"云端识别服务请求失败（HTTP {status}）。")


def _completion(request: Request, api_key: str, max_length: int) -> str:
    try:
        with build_opener(_NoRedirect()).open(request, timeout=_TIMEOUT) as response:
            data = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        exc.close()
        raise _status_error(exc.code) from None
    except (TimeoutError, URLError) as exc:
        if isinstance(exc, TimeoutError) or isinstance(exc.reason, TimeoutError):
            raise CaptionRuntimeError("云端识别请求超时，请稍后重试。") from None
        if isinstance(exc.reason, ssl.SSLError):
            raise CaptionRuntimeError("云端 HTTPS 连接失败，请检查证书、代理和网络。") from None
        raise CaptionRuntimeError("无法连接云端识别服务，请检查地址和网络。") from None
    except (OSError, HTTPException, ValueError):
        raise CaptionRuntimeError("无法连接云端识别服务，请检查地址和网络。") from None

    if len(data) > _MAX_RESPONSE_BYTES:
        raise CaptionRuntimeError("云端识别响应超过大小限制。")
    try:
        text = json.loads(data)["choices"][0]["message"]["content"]
        if isinstance(text, list):
            text = " ".join(part["text"] for part in text if part.get("type") == "text")
        text = " ".join(text.split()).strip("` \t")
        if not text or "data:image/" in text or api_key in text:
            raise ValueError
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, RecursionError):
        raise CaptionRuntimeError("云端模型没有返回可用的识别描述。") from None
    return text[:max_length]


def _cloud_model_key(model: str, api_key: str) -> tuple[str, str]:
    if not isinstance(model, str) or not model.strip() or len(model.strip()) > 256:
        raise CaptionRuntimeError("请填写云端模型名称。")
    if not isinstance(api_key, str) or not api_key.strip() or any(not 33 <= ord(c) <= 126 for c in api_key.strip()):
        raise CaptionRuntimeError("请填写有效的云端 API Key。")
    return model.strip(), api_key.strip()


def _chat_request(endpoint: str, model: str, api_key: str, messages: list[dict], *, max_length: int) -> str:
    payload = {"model": model, "messages": messages}
    request = Request(endpoint, data=json.dumps(payload).encode("ascii"), method="POST", headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    return _completion(request, api_key, max_length)


def generate_cloud_caption(
    paths: list[Path], kind: str, role: str, *, base_url: str, model: str,
    api_key: str, instructions: str | None = None,
) -> str:
    """Describe saved media using an explicitly configured HTTPS provider."""
    endpoint = _chat_endpoint(base_url)
    model, api_key = _cloud_model_key(model, api_key)
    paths = _media_paths(paths, kind, role)
    custom = _custom_instructions("image_prompt" if kind == "images" else f"{role}_video_prompt", instructions)
    prompt = _visual_prompt(kind, role, custom, combined=True)
    if kind == "video":
        with tempfile.TemporaryDirectory(prefix="caption-frames-") as temp:
            frames = _video_frames(paths[0], *_video_tools(), output_dir=Path(temp))
            images = _image_content(frames)
    else:
        images = _image_content(paths)
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}, *images]}]
    return _chat_request(endpoint, model, api_key, messages, max_length=240)


def generate_cloud_page_caption(
    page_title: str, page_text: str, *, base_url: str, model: str, api_key: str, instructions: str | None = None,
) -> str:
    """Summarize public product evidence using the selected HTTPS model."""
    endpoint = _chat_endpoint(base_url)
    model, api_key = _cloud_model_key(model, api_key)
    messages = _page_caption_messages(page_title, page_text, instructions)
    description = _chat_request(endpoint, model, api_key, messages, max_length=MAX_PAGE_CAPTION_LENGTH)
    return _page_description(description)
