# 豆包多账号视频生成自动化

项目目标是在多个独立浏览器配置中完成：**脚本文案 + 产品图片 → 豆包生成视频**，
Apple Silicon Mac 已完成视频生成主流程的实机验证；第三方审核后处理依赖尚未纳入。验证范围与修复说明见 [Mac 修复记录](docs/macos-fixes.md)：

```
飞书多维表格                     Gemini 网页版                  豆包账号池
┌─────────────────┐   视频+提示词+固定指令   ┌────────────┐   每个脚本(切分为≤15s多段)   ┌──────────────┐
│ 产品图表: 产品图  │ ────────────────────► │ 生成多个脚本 │ ─────────────────────────► │ 对话框输入文案 │
│ 提示词表: 提示词/视频│      (浏览器自动化)    └────────────┘   首段: 产品图             │ + 上传图片     │
└─────────────────┘                                    后续段: 上一段尾帧作首帧 ──────► │ 生成并下载视频 │
      (飞书 OpenAPI)                                                                    └──────────────┘
                                                       确认额度耗尽 → 尝试下一个可用账号
```

## 关键机制

长视频与额度规则的设计见 [长视频生成与账号额度规则](docs/long_video_rules.md)：每段最多15秒、每批最多2条；本地账本按配置的每日额度跟踪已用、预留和未决记录。网页结构、额度和响应格式可能变化；多账号切换及更多页面分支仍需独立实测。

- **15 秒限制**：脚本按中文口播语速（默认 4.5 字/秒）估算时长，超过 `video.target_segment_seconds`
  （默认 13 秒，留缓冲）就自动切成多段并改写每段文案（`doubao_automation/script_splitter.py`）。
- **首尾帧无缝衔接**：
  1. 每段发给豆包的提示词都显式写明「首帧画面 = 上一段尾帧画面」的定格描述（文字层面锁死光线/机位/产品位置）；
  2. 上一段视频下载后立刻用 ffmpeg 提取**真实尾帧**，作为下一段上传的第一张图，并要求豆包「以该图为首帧」；
  3. 流水线会尝试对满足条件的分段进行合成；`video.auto_concat` 不是当前代码读取的开关，不能用它控制合成。
- **多账号与额度切换**：账号由使用者在本机配置或工作台添加并人工登录，不固定数量。明确识别额度耗尽时会尝试启用账号中的下一个；网络、登录、验证等含糊提示不会判为耗尽。`doubao.quota_exhausted_patterns` 可按真实页面提示调整，空列表禁用文本识别；余额元素需用户校准 `doubao.selectors.quota_balance`。默认中英文模板尚未获得真实平台样本验证，无可用账号或结果不明确时保留任务状态，不保证自动续跑成功。
- **断点续跑**：所有任务状态持久化在 `workspace/state/pipeline_state.json`，中断后重跑 `run` 自动续接。
- **登录问题**：检测到登录失效时暂停该账号并提示；随时可重跑 `python run_login.py` 弹窗人工登录。

## 目录结构

```
├── run_login.py            # 按本机配置打开登录窗口
├── run_pipeline.py         # 流水线入口（fetch/scripts/plan/run/status/inspect-*/concat）
├── config/config.example.yaml # 无敏感数据的配置模板；私有配置由用户自行创建
├── start_mac.command       # macOS 原生入口，与 scripts/start_macos.command 相同
├── docs/macos.md           # macOS 安装、配置与验证范围
├── doubao_automation/            # 各模块（浏览器池/飞书/Gemini/切分/豆包代理/编排）
├── profiles/               # Windows 运行时数据；macOS 存放在用户数据目录
└── workspace/              # Windows 运行时数据；macOS 存放在用户数据目录
```

## 平台入口

- **Windows**：安装 Python、将完整 FFmpeg（含 FFprobe）加入 PATH，创建 `.venv` 并安装依赖，然后双击 `scripts/start_windows.bat`。登录状态和任务数据使用 Windows 本机数据目录。
- **macOS（Apple Silicon）**：先安装 Python 3.10+ 和完整 FFmpeg（含 FFprobe），再双击根目录 `start_mac.command` 或 `scripts/start_macos.command`；两者使用同一原生流程，自动准备缺失的虚拟环境、依赖及可用浏览器，**无需 Docker**。私有配置按需初始化且不覆盖。详见 [Mac 安装与启动指南](docs/macos.md)。Intel Mac 尚未实机验证。
- **关闭与历史清理**：`scripts/stop_windows.bat`、`scripts/stop_macos.command` 目前会明确提示安全关闭尚未支持，不终止进程。两个 `clear_history` 入口和工作台按钮目前仅提供保守预览，不删除记录或文件；需要先完成原子关停、引用核验和恢复机制才能支持实际操作。以上入口均尚未实测。
- **Docker**：Linux Web/API 预览与 Mac 原生运行分开，见 [Docker 说明](docs/docker.md)；不包括浏览器和后处理能力，镜像访问权限需单独核实。
- **维护与发布**：发布前阅读 [GitHub 与 GHCR 指南](docs/publishing.md)，核对仓库根目录、远端、隐私与测试；不上传本机配置或运行数据。
- **Linux**：保留平台隔离设计，尚未提供经过验证的部署入口或 Docker 镜像。
## 首次安装（在项目根目录执行）

### 本地仪表盘

Windows 双击 `start_dashboard.bat`；Mac 双击 `start_mac.command`。启动器在 HTTP、页面内容及项目/数据目录归属检查通过后打开 `http://127.0.0.1:8782/`，重复启动复用同一个常驻管理服务。启动控制台不会自动提交生成任务。

- “同步飞书”从远端读取素材；“保存修改”写入本地状态，不回写飞书。运行使用已保存素材，不会重新同步覆盖修改。
- 素材行的“删除”只移出当前列表；普通“同步飞书”会恢复远端仍存在且成功导入的记录。产品图库与素材列表独立显示，同步统计说明空行、缺少附件等跳过情况。
- 脚本数量根据素材提示词及 Gemini 返回的脚本确定。操作消息与实时运行日志分别显示，失败时会显示未完成。
- 已下载视频的“打开文件”在浏览器直接播放，支持拖动进度；“打开文件夹”请求 Windows 打开视频目录，按钮旁显示请求结果或系统错误。
- 当前成片显示脚本与片段序号；对应成片存在时不重复列出 raw 下载，原文件仍保留。
- 每行的“删除”经确认后会删除该行原始 MP4 的磁盘文件，列表随即移除该视频；排队或后处理中的视频暂不能删除。
- 添加豆包账号后会打开独立浏览器，需要人工登录。新账号在登录验证成功前不会进入运行账号池；可关闭登录窗口后使用其他已登录账号运行。
- 每次运行使用独立编号保存新视频，保留已有成片。运行日志保存在 `logs/r*.log`；启动故障查看 `logs/dashboard.log`。

后台管理进程保存在 `dashboard_service.py`。它会监测仪表盘和 `doubao_automation/*.py` 的修改；由于尚无原子安全关停协议，检测到变更时只标记待重载，**不会自动结束或重载工作进程**。管理进程退出后，工作进程可能仍在运行，状态文件也可能过期；不能仅凭状态文件判断进程已退出。运行状态见数据目录的 `.tmp/dashboard_service.json`，管理日志见 `logs/dashboard_service.log`。页面 JavaScript 修改通常需要刷新页面。Mac 启动复用已核验，安全关闭与历史删除仍保留上述限制。

Windows 桌面助手与 macOS Finder 操作是不同的平台路径：Windows 新启动入口可在默认数据目录下请求启动桌面助手；自定义数据目录时不会启动不匹配的助手。macOS 通过 Finder 请求显示文件，无法证明窗口已显示；两平台均需实机核验。

### 审核后自动去水印与超分

**本发布副本不包含 video-subtitle-remover、RapidOCR、后处理适配器或模型权重**，因此审核后的去水印与 SeedVR2 超分功能尚不能运行；未经 PATH-only FFmpeg 验证的第三方适配器会被拒绝，补装模型本身无法启用。ComfyUI 需由使用者独立部署，节点、模型和 GPU 兼容性仍需验证。视频生成完成不代表审核后处理已完成，也不保证提示词所写的 4K/60fps 或无水印规格。
命令行使用工作台已保存素材：`python run_pipeline.py all --from-saved --force`。普通 `all --force` 仍会重新同步飞书。

以下环境准备命令面向 Windows；Mac 的首次准备由原生入口执行，见 [Mac 指南](docs/macos.md)。

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# 使用系统 Chrome（推荐，无需下载浏览器）；若没有 Chrome 才需要:
# python -m playwright install chromium
```

## 飞书应用配置（一次性）

同步需要使用者自己的私有配置，下载源码后不会自动携带任何飞书账号、密钥或提示词。请勿把 `config/config.yaml`、固定提示词或 Cookie 上传到 GitHub。

Windows 在项目目录下执行以下命令准备配置；如果文件已存在，请保留原文件并手动编辑，不要覆盖：

```powershell
if (-not (Test-Path config/config.yaml)) { Copy-Item config/config.example.yaml config/config.yaml }
notepad config/config.yaml
```

macOS 启动器会按 [Mac 配置说明](docs/macos.md#数据目录与私有配置)初始化缺失模板，并打印实际配置路径；已有私有配置不覆盖。若设置了 `DOUBAO_DATA_DIR` 或 `DOUBAO_CONFIG_PATH`，请编辑该设置对应的本机配置，而不是修改示例文件。

1. 打开 [飞书开放平台](https://open.feishu.cn/app) → 创建**自建应用**；
2. 权限管理中开通：`bitable:app:readonly`（查看多维表格）、`drive:drive:readonly`（下载云空间文件）；
3. 创建版本并**发布**（企业管理员审核通过后生效）；
4. 在目标多维表格 Base 右上角「…」→ 添加文档应用 → 选择该应用（可阅读权限即可）；
5. 在私有配置填写 `feishu.app_id`、`app_secret`、多维表格 Base 的 `app_token`，以及 `products_table.table_id` 和 `scripts_table.table_id`；表中的字段名也要与实际列名一致。两个表需要允许该应用读取；
6. 默认固定提示词来自私有的 `config/gemini_prompt.txt`，请自行创建并填写。macOS 的相对路径位于应用数据目录；若存在飞书文档/表格，也可把 `feishu.fixed_prompt` 改为对应来源
   （`bitable`: table_id+field+record_index；`docx`: document_id）。

点击“同步飞书”后，按钮旁会显示进行中、成功或失败原因。附件下载可能需要较长时间；如出现权限错误，请检查应用是否已发布、授权是否生效，以及 Base 是否已将应用加入协作者。同步失败不会自动补充私有配置。

若 `feishu.fixed_prompt` 使用素材表的同一提示词列，且 `record_index: -1`，每条素材只使用本行提示词，避免混入另一产品的内容。共用模板应放在独立文档、独立表，或明确指定固定记录。飞书 TLS 认证错误应检查本机网络与代理节点；程序保持证书校验并有限重试，不会通过关闭 TLS 校验修复网络问题。

## 使用流程

下文 `python` 指项目虚拟环境的解释器；Mac 可直接使用 `.venv/bin/python`，Windows 可使用 `.venv\Scripts\python.exe`。登录、验证码和新协议由使用者在可见浏览器内完成，不绕过服务的权限和额度限制。

```powershell
# ① 登录：按本机配置打开账号窗口，人工完成登录
python run_login.py

# ② 全流程：飞书下载 → Gemini 生成完整脚本 → 最长15秒分段 → 按账号余额依次生成
python run_pipeline.py all

# 分步执行 / 排查
python run_pipeline.py fetch --force     # 重新拉飞书数据
python run_pipeline.py scripts --limit 1 # 只用第 1 条源记录试跑 Gemini
python run_pipeline.py plan              # 查看切分结果（生成任务）
python run_pipeline.py run               # 执行/续跑
python run_pipeline.py status            # 任务与账号状态
python run_pipeline.py concat --script s0_1
```

产物位置：分段视频 `workspace/videos/`，尾帧 `workspace/frames/`，脚本 `workspace/scripts/`。

## 页面选择器校准（豆包/Gemini 改版时）

所有选择器都集中在 `config/config.yaml`，无需改代码。实机勘察：

```powershell
python run_pipeline.py inspect-doubao --account doubao1 --interactive
python run_pipeline.py inspect-gemini
```

会把页面截图 + 可交互元素清单存到 `workspace/inspect/`，据此更新 config.yaml 即可。

## 常见问题

- **登录后仍显示等待**：程序需要有效会话和可用聊天输入区。先完成人工登录或页面中的协议步骤，再查看登录状态和具体错误；关闭窗口或访客 Cookie 不代表登录成功。网页改版时用 inspect 校准，不要公开 Cookie 值。
- **提示「找不到对话输入框 / 上传入口」**：运行对应 inspect 命令校准选择器。
- **某账号被标记 needs_login**：重跑 `python run_login.py --account doubao2` 登录后，
  `python run_pipeline.py run` 自动续跑。
- **额度跨天恢复**：耗尽账号次日自动回到可用池，无需手工干预。
- **批次与额度**：当前按账号依次执行，每批最多两条、每账号每天最多四条；余额为零后切换已登录账号。
  已提交但结果未知的请求只恢复原对话，不能通过重新登录补回次数。

## 已知边界

- 豆包/ Gemini 均为网页自动化，页面大改版时需要按上面方法校准选择器；
- Gemini 网页版对上传视频大小有限制（以页面实际提示为准），超大视频请先压缩；
- 豆包对同一账号的免费视频生成次数以官方为准，本工具只做「检测 + 切换」，不绕过任何限制。
