#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RUNTIME="$ROOT/.tmp/caption-runtime"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
    echo '本地识别需要 Apple 芯片的 Mac。' >&2
    exit 1
fi
MACOS=$(sw_vers -productVersion)
if [ "${MACOS%%.*}" -lt 26 ]; then
    echo '当前版本的本地识别依赖 macOS 26 或更新版本。' >&2
    exit 1
fi
if ! python3.12 -I -c 'import platform; raise SystemExit(platform.machine() != "arm64")' 2>/dev/null; then
    echo '请先安装 arm64 Python 3.12。' >&2
    exit 1
fi

mkdir -p "$RUNTIME"
# A second invocation inherits the locked descriptor from the first.
if ! python3.12 -I - "$RUNTIME/inference.lock" <<'PY'
import fcntl
import os
import sys

try:
    with open(sys.argv[1], "a") as lock:
        if not os.path.sameopenfile(9, lock.fileno()):
            raise SystemExit(1)
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    raise SystemExit(1) from None
PY
then
    exec python3.12 -I - "$RUNTIME/inference.lock" "$ROOT/scripts/setup_caption_runtime.sh" <<'PY'
import fcntl
import os
import sys

with open(sys.argv[1], "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("本机正在识别素材或安装模型，请等待完成。") from None
    os.dup2(lock.fileno(), 9)
    os.set_inheritable(9, True)
    os.execv("/bin/sh", ["/bin/sh", sys.argv[2]])
PY
fi
if [ ! -x "$RUNTIME/venv/bin/python" ]; then
    python3.12 -m venv "$RUNTIME/venv"
fi
PYTHON="$RUNTIME/venv/bin/python"
if command -v uv >/dev/null 2>&1; then
    UV_CACHE_DIR="$RUNTIME/uv-cache" uv --no-config pip install --python "$PYTHON" \
        --default-index https://pypi.org/simple 'mlx-vlm==0.7.12'
    uv pip check --python "$PYTHON"
else
    "$PYTHON" -m pip --isolated install --index-url https://pypi.org/simple \
        --cache-dir "$RUNTIME/pip-cache" 'mlx-vlm==0.7.12'
    "$PYTHON" -m pip check
fi

HF_HOME="$RUNTIME/hf-cache" HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    "$PYTHON" -I - "$ROOT" "$RUNTIME" <<'PY'
import hashlib
import json
import runpy
import sys
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

root, runtime = map(Path, sys.argv[1:])
constants = runpy.run_path(str(root / "doubao_automation" / "caption_runtime.py"))
repo = constants["_MODEL_REPO"]
revision = constants["_MODEL_REVISION"]
expected_sha256 = constants["_MODEL_SHA256"]
required = constants["_REQUIRED_FILES"]
model = runtime / "model"
model.mkdir(exist_ok=True)
endpoint = "https://huggingface.co"
try:
    metadata = HfApi(endpoint=endpoint).model_info(repo, revision=revision, files_metadata=True, token=False)
except Exception as exc:
    raise SystemExit(f"读取官方模型版本失败（{type(exc).__name__}），请检查下载连接。") from None
if metadata.sha != revision:
    raise SystemExit("官方模型版本校验失败。")


def verified(path, item):
    if not path.is_file() or path.stat().st_size != item.size:
        return False
    with path.open("rb") as data:
        if item.lfs:
            return hashlib.file_digest(data, "sha256").hexdigest() == item.lfs.sha256
        digest = hashlib.sha1(f"blob {item.size}\0".encode())
        digest.update(data.read())
        return digest.hexdigest() == item.blob_id


files = {}
for item in metadata.siblings:
    name = item.rfilename
    if Path(name).name != name or Path(name).suffix not in {".safetensors", ".json", ".jinja", ".txt"}:
        continue
    if name == "model.safetensors" and (not item.lfs or item.lfs.sha256 != expected_sha256):
        raise SystemExit("官方权重 SHA-256 校验失败。")
    path = model / name
    if not verified(path, item):
        print(f"下载并校验 {name}", flush=True)
        try:
            hf_hub_download(repo, name, revision=revision, local_dir=model, token=False,
                            endpoint=endpoint, force_download=path.exists())
        except Exception as exc:
            raise SystemExit(f"下载 {name} 失败（{type(exc).__name__}），请检查下载连接。") from None
        if not verified(path, item):
            raise SystemExit(f"模型文件校验失败：{name}")
    files[name] = item.size

if not required <= files.keys():
    raise SystemExit("模型缺少推理所需文件。")
manifest = runtime / "model-manifest.json"
pending = manifest.with_suffix(".json.part")
pending.write_text(json.dumps({"repo": repo, "revision": revision, "weight_sha256": expected_sha256, "files": files}, indent=2))
pending.replace(manifest)
print(f"本地识别模型已安装并通过校验。版本 {revision}")
PY
