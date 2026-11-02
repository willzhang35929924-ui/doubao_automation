# macOS 原生运行指南

本指南面向 Apple Silicon（M1/M2/M3/M4），当前版本**尚未在 macOS 或 Apple Silicon 实机验证**。网页选择器可能随豆包、Gemini 页面变化；首次运行前需要自行校准。无需复制 Windows 浏览器资料或 Cookie。

## 环境准备

1. 使用受支持的 macOS 和原生 ARM64 Python（建议 Python 3.12），可用 `uname -m` 和 `python3 --version` 检查。安装 [Homebrew](https://docs.brew.sh/Installation) 后，按安装器提示配置 shell PATH。
2. 安装系统 FFmpeg（包含 FFprobe），并确认终端可以从 PATH 找到两者：

   ```sh
   brew install ffmpeg
   command -v ffmpeg
   command -v ffprobe
   ```

3. 在项目目录中创建虚拟环境并安装 Python 依赖及 Playwright Chromium。浏览器安装是使用者在自己的 Mac 上执行的操作，本发布副本不携带浏览器或虚拟环境：

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m playwright install chromium
   ```

   macOS 默认使用 Playwright 管理的 Chromium；Windows 默认仍使用既有系统 Chrome 配置。有关浏览器安装参见 [Playwright 官方文档](https://playwright.dev/python/docs/browsers)。

## 私有配置与登录

默认数据位置是 `~/Library/Application Support/DoubaoAutomation/`。配置、profile、任务文件、缓存及日志保存在该用户数据目录，而非源码目录。可设置绝对路径环境变量 `DOUBAO_DATA_DIR` 更改数据目录，或通过 `DOUBAO_CONFIG_PATH` 指向独立的私有 YAML 配置。

复制不含密钥和账号的模板到私有位置，按自己的飞书表格、提示词和网页实际情况填写；不要把私有配置提交或分享：

```sh
mkdir -p "$HOME/Library/Application Support/DoubaoAutomation/config"
if [ ! -e "$HOME/Library/Application Support/DoubaoAutomation/config/config.yaml" ]; then
  cp config/config.example.yaml "$HOME/Library/Application Support/DoubaoAutomation/config/config.yaml"
else
  printf '%s\n' '私有配置已存在，未覆盖。'
fi
```

模板的账号列表为空。通过工作台添加豆包账号；Gemini 在私有 YAML 的 `accounts.gemini` 中至少需要 `name` 和 `profile`，例如通用本地标签和相对数据目录：

```yaml
accounts:
  doubao: []
  gemini:
    name: local-gemini
    profile: profiles/local-gemini
```

其中标签不代表登录账号。浏览器中的登录、验证码和新协议由使用者手动完成。登录验证规则及网页选择器可能因平台版本变化需要在私有配置中补充；缺少规则时页面会明确提示，不能视为已登录。profile 不应从其他设备复制。

需要飞书同步时，在私有 YAML 的 `feishu` 项填写自己的配置；若只使用工作台手动素材，不必将真实飞书凭据写入模板或源码。固定提示词由使用者自行提供，发布副本不含原始私有提示词。

## 启动

在 Finder 双击 `scripts/start_macos.command`；它调用与 Windows 相同的 Python 启动控制逻辑。也可以在项目目录运行：

```sh
bash scripts/start_macos.sh
```

首次使用可能需要为 `.command` 和 `.sh` 设置可执行权限，例如 `chmod +x scripts/*.command scripts/*.sh`。Git 不通过 `.gitattributes` 保存可执行位；维护者需在首次提交前检查并为需要的脚本记录执行位（例如使用 `git update-index --chmod=+x`）。这些命令需由使用者自行执行。

`scripts/stop_macos.command` 当前只报告安全关闭尚未支持，不会终止进程。`scripts/clear_history_macos.command` 和工作台历史清理入口目前仅提供保守预览，不会删除数据。启动、关闭和清理入口均未在 Mac 上实测。

首次运行浏览器可能请求用户确认或系统权限；按系统提示处理。工作台通常使用本机回环地址 `http://127.0.0.1:8782/`；若端口已被其他程序占用，先检查并停止冲突程序。不要将本地服务暴露到不可信网络。

## ComfyUI 和审核后处理

ComfyUI 与 SeedVR2 由使用者自行部署、配置并运行；可通过 `DOUBAO_COMFY_URL` 指定 HTTP 服务地址。本地可使用仅供示例的 `http://127.0.0.1:8188`，实际端口以用户自己的服务设置为准。后处理还需要完整工作流配置，默认读取用户数据目录的 `postprocess_setup/seedvr_config.json`，也可用 `DOUBAO_SEEDVR_CONFIG` 指定；仅设置服务地址不能替代工作流配置。配置中的模型路径、工作流、节点和依赖由使用者自行核实，不应提交。

**本发布副本不包含 video-subtitle-remover、RapidOCR、模型权重或后处理适配器。** 审核后去水印与 SeedVR2 超分目前无法运行，未经 PATH-only FFmpeg 验证的第三方适配器会被拒绝；不要根据已有旧项目日志推断 Mac 可用；节点兼容、MPS/GPU 支持和完整链路均待真实 Apple Silicon Mac 验证。

## 常见问题

- **找不到 ffmpeg / ffprobe**：先运行 `brew install ffmpeg`，确认 Homebrew 的 shell 环境已加入 PATH，再分别执行 `command -v ffmpeg`、`command -v ffprobe`。
- **找不到浏览器**：在项目虚拟环境中重新执行 `.venv/bin/python -m playwright install chromium`；不要把浏览器文件放进仓库。
- **配置不存在**：检查 `DOUBAO_CONFIG_PATH`、`DOUBAO_DATA_DIR` 与私有配置路径；示例模板不会自动生成真实配置。
- **登录一直等待**：检查用户是否已完成手动登录、私有配置中的检测规则及页面选择器；不要粘贴或公开 Cookie 值。
- **无法打开文件夹**：检查 macOS 文件访问与桌面会话权限；具体 Finder 行为待实机验证。
- **Apple Silicon 推理问题**：本项目未随附后处理依赖，也未验证 SeedVR2 的 MPS 或任何特定 GPU 加速支持。
