# macOS 原生运行指南

Mac 端使用本机 Python、Playwright 浏览器和 FFmpeg，**不需要 Docker、GHCR 镜像或 Compose 配置**。项目根目录的 `start_mac.command` 与 `scripts/start_macos.command` 调用同一套原生启动流程。

2026-11-04 已在 Apple Silicon ARM64 Mac 上完成视频生成主流程的真实验证：4 条 15 秒、720P、24fps 视频，逐条核对画面、解码和 HTTP 播放。该轮是在运行中修复问题后继续原请求完成，并非从零无人干预跑完。Intel Mac 尚未实机验证；更多机型、多账号切换和网页未来版本仍需独立验证。修复范围见 [Mac 修复记录](macos-fixes.md)。

## 首次启动

1. 安装 Python 3.10 或更新版本，建议使用 Python 3.12。Python 应与当前 Mac 架构匹配，不要从其他电脑复制 `.venv`。
2. 安装完整 FFmpeg，确保同时有 `ffmpeg` 与 `ffprobe`。已使用 Homebrew 的环境可执行：

   ```sh
   brew install python@3.12 ffmpeg
   command -v python3
   command -v ffmpeg
   command -v ffprobe
   ```

   Homebrew 本身的安装按其[官方说明](https://docs.brew.sh/Installation)处理；启动器不会自动安装 Homebrew 或修改系统软件。

3. 将完整源码目录放在当前用户可写的位置，在 Finder 双击根目录 `start_mac.command`，也可双击 `scripts/start_macos.command`。终端入口为：

   ```sh
   /bin/sh scripts/start_macos.sh
   ```

首次启动会创建缺失的项目 `.venv`、安装 `requirements.txt` 依赖，并检查可用浏览器。默认优先使用匹配的 Playwright Chromium；若已有可用系统 Chrome，则以独立 Profile 复用。缺少可用浏览器且配置允许时会安装匹配的 Chromium。安装有超时限制，需要可访问 Python 包源和 Playwright 官方下载服务。系统依赖缺失或下载失败时报告错误，不会当成启动成功。

配置缺失时会从无密钥模板初始化，已有配置不覆盖。启动窗口会打印实际配置文件路径。若 ZIP 解压后丢失执行权限，可使用上面的 `/bin/sh` 入口，或恢复脚本权限：

```sh
chmod +x start_mac.command scripts/start_macos.command scripts/start_macos.sh
```

维护者会在 Git 中记录这些入口的执行位；它与 `.gitattributes` 的换行设置是两回事。

## 数据目录与私有配置

启动器按以下顺序选择本机配置和数据目录：

1. 尊重用户显式设置的绝对路径 `DOUBAO_DATA_DIR` 和 `DOUBAO_CONFIG_PATH`。
2. 两者均未设置、且项目中已有 `config/config.yaml` 时，复用项目内配置与数据，避免误用另一个空模板。
3. 其他新安装默认使用 `~/Library/Application Support/DoubaoAutomation/`。

配置、浏览器 Profile、任务状态、视频、临时文件和日志均属于本机私有数据，不随源码分发。相对 `profiles/`、`workspace/`、`config/` 等运行路径相对于选定的数据目录解析。可用以下方式明确指定自己的数据目录：

```sh
DOUBAO_DATA_DIR="$HOME/DoubaoAutomationData" /bin/sh scripts/start_macos.sh
```

`DOUBAO_CONFIG_PATH` 只指定独立的私有 YAML 文件；需要同时改变数据位置时，一并指定 `DOUBAO_DATA_DIR`。不要把真实密钥、Cookie、Profile 或素材提交到 GitHub。

首次初始化的配置文件权限为 `600`，模板账号列表为空。模板只让控制台具备可编辑的配置结构，**不会代替使用者填写飞书凭据、提示词或登录账号**。

## 登录、飞书与生成

在账号库添加豆包账号后，程序打开该账号独立的可见浏览器；Google/Gemini 使用自己的本地 Profile。登录、验证码、系统权限和新协议必须由使用者完成，不会绕过。完成登录后程序会验证实际会话和可用输入区；仅出现访客输入框不会被当作登录成功。不要从其他设备复制 Profile 或 Cookie。

`accounts.gemini` 可使用默认值，也可在私有配置指定自己的通用本地标签和相对路径，例如：

```yaml
accounts:
  doubao: []
  gemini:
    name: local-gemini
    profile: profiles/local-gemini
```

需要飞书同步时，按 [README 的飞书配置](../README.md#飞书应用配置一次性)填写应用与表格信息。默认固定指令来源为私有 `config/gemini_prompt.txt`，使用前需自行填写该文件，或将来源改为自己的飞书文档/表格；仓库不包含业务提示词。

若固定指令设为素材表的同一个“提示词”列、自动记录模式 `record_index: -1`，每条素材只读取自己的行提示词；不会把该列第一条非空内容混到其他产品中。若需要所有素材共用的模板，请使用独立文档、独立表，或明确指定一条固定记录。

素材行的“删除”只移出当前工作台；下一次普通“同步飞书”会恢复远端仍存在、且本次成功导入的记录。产品图库与素材列表分开显示；空行、缺少附件等情况通过同步统计说明。保存修改只保存本地状态，不回写飞书。

“开启运行”会提交真实生成任务并可能消耗服务额度。中断后使用原计划续跑，已提交但结果未知的请求只恢复原会话；不要为解决等待或下载错误反复强制新建任务。新生成产物按原用户消息和视频卡片身份匹配，恢复时验证本地文件绑定和哈希。

## 服务复用与故障定位

启动器只在真实 HTTP 检查、页面内容以及项目/数据目录归属通过后打开 `http://127.0.0.1:8782/`。重复启动会复用同一个经过归属验证的后台服务，不会自动提交新一轮生成。控制台只监听本机回环地址，不向局域网公开。

启动窗口会显示失败原因、实际配置路径与 `<数据目录>/logs/macos_startup.log`。交互式 `.command` 启动失败时保留终端提示，终端 `.sh` 入口则以非零退出码返回。其他应用日志也位于该数据目录的 `logs/`：启动检查 `dashboard_launcher.log`、`dashboard_service.log` 和 `dashboard.log`，任务运行查看 `r*.log`。网页 JavaScript 更新后需刷新页面；代码更新会报告待重载，不会为更新强制结束进行中的任务。

常见情况：

- **找不到 Python / FFmpeg / FFprobe**：安装上述系统依赖。启动器会补充常见 Apple Silicon 与 Intel Homebrew 路径，也保留用户 PATH。
- **浏览器安装或依赖下载失败**：检查包源、下载服务和本机网络后重新启动；也可在项目虚拟环境运行 `.venv/bin/python -m playwright install chromium`。不要把浏览器或 `.venv` 上传到仓库。
- **飞书认证出现 TLS EOF**：检查本机代理、节点和网络。程序会有限重试并保留证书校验，不会自动关闭代理或设置 `verify=False`；每个人应维护自己的网络配置。
- **端口被占用或服务归属不一致**：先核对占用方；启动器会拒绝复用其他项目，不会结束无关程序。
- **原请求等待或结果不明确**：查看日志和原对话；程序会保留未决状态，不以错误的旧视频或重复提交冒充成功。

`scripts/stop_macos.command` 当前只报告安全关闭尚未支持，不会终止进程。历史清理入口目前仅提供保守预览，不删除数据。不要通过删除浏览器 Profile 或任务账本来解决启动问题。

## 审核后处理的边界

本仓库不包含 video-subtitle-remover、RapidOCR、模型权重或已验证的后处理适配器。审核后的去水印与 SeedVR2 1080P 超分尚不能据此宣称可运行；补装模型本身不能启用未经验证的适配器契约。

ComfyUI、工作流、节点、模型路径及 GPU/MPS 支持由使用者独立部署和验证。可通过 `DOUBAO_COMFY_URL` 与 `DOUBAO_SEEDVR_CONFIG` 指定自己的服务和配置，但仅填写服务地址不能替代完整适配器与工作流。不要提交模型或私有工作流配置。

2026-11-04 的实测产物是平台生成的 720P/24fps 视频，保留平台生成标识；该结果不等于提示词所写的 4K/60fps 或无水印成片。
