# Docker Web/API 预览（未构建、未验证）

此方案仅供学习 Linux 容器部署。镜像不含浏览器、登录状态、模型、ComfyUI、SeedVR2、VSR 或 RapidOCR。容器预览禁用启动生成任务、添加或打开账号、打开桌面文件夹和提交后处理；不要将其视为完整自动化服务。历史清理能力以工作台实际提示为准。

## 网络边界

现有工作台只监听 `127.0.0.1:8782`，并校验访问方回环地址、Host、Origin 和写请求 CSRF 令牌。普通桥接端口转发会改变来源地址，不能直接满足现有检查。本方案使用 Linux Engine 主机网络，让程序继续绑定宿主机回环地址，不发布公网端口；因此它共享宿主网络栈，不能提供独立网络隔离。端口已被占用时不会启动第二个工作台。

Docker Desktop 的主机网络有额外设置与平台限制，本项目未验证 Windows 或 macOS 下的行为。不要通过关闭访问检查或改为监听所有网卡来解决连接问题。有关范围与限制请参阅 [Docker 主机网络文档](https://docs.docker.com/engine/network/drivers/host/)。

## 仅供后续手动操作

需要 Linux Docker Engine、Compose 和网络访问来构建依赖。先复制 `.env.example` 为本机 `.env`，自行填写可选服务地址。`DOUBAO_COMFY_URL` 只是服务地址，不能使缺失的模型、节点和后处理链路自动可用。

首次启动前需要在命名卷 `/data/config/config.yaml` 中准备私有配置。下面的命令仅在不存在配置时从无密钥模板创建，实际字段需使用者自行填写：

```sh
cp -n .env.example .env
docker compose build
docker compose run --rm --entrypoint python dashboard -c "from pathlib import Path; import shutil; p=Path('/data/config/config.yaml'); p.parent.mkdir(parents=True,exist_ok=True); p.exists() or shutil.copyfile('/app/config/config.example.yaml',p)"
docker compose up -d
```

容器健康检查仅检查首页能否访问，不代表配置、账号或生成流程可用。停止使用 `docker compose stop`。命名卷包含私有数据；不要上传或分享，也不要使用会删除卷的命令，除非使用者明确决定删除自己的数据。

## 架构与验证状态

Dockerfile 使用 Python 3.12 slim 和发行版 FFmpeg/FFprobe，Python 依赖来自 `requirements.txt`。[Playwright 官方支持列表](https://playwright.dev/python/docs/intro)包含部分 Debian/Ubuntu x86-64 与 ARM64，且其 [PyPI 发布文件](https://pypi.org/project/playwright/)包含 Linux 两种架构的包；这不等于本项目的依赖组合已验证。Playwright 的 Python 包不等于已安装浏览器；本镜像未安装浏览器。`linux/amd64` 和 `linux/arm64` 的基础镜像、APT 包和 Python 依赖需要实际构建核对，此处不声称任一架构已构建或验证。宿主机 ComfyUI、节点、模型以及 GPU 功能均需独立部署验证。

未来可在人工检查后使用 `docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/OWNER/doubao-automation:TAG --push .`；替换 `OWNER` 和 `TAG`，并先检查自己的 GHCR 授权。此命令会构建并推送，本次工作未执行。
