# GitHub 与镜像维护

本仓库发布源码、无密钥模板和测试。维护者发布前应确认真实仓库根目录、目标远端与改动范围，保留本地未提交内容。已有仓库不需要重新 `git init`。

## 整合与检查

在项目目录检查仓库和远端，再获取最新分支；不要假定外层文件夹就是仓库根目录：

```sh
git rev-parse --show-toplevel
git remote -v
git status --short --untracked-files=all
git fetch origin
git log --oneline HEAD..origin/main
```

远端有其他提交时，先整合并解决冲突，保留双方有效修改。可使用同仓库的隔离分支或 worktree 准备发布，避免影响正在运行的本地服务；不要以 `reset --hard`、删除用户数据或强制推送处理分歧。

发布内容应显式列出。只暂存本次审核过的源码、测试和通用文档，例如：

```sh
git add README.md docs/macos.md scripts/start_macos.sh
git diff --cached --stat
git diff --cached --check
git diff --cached
```

以上只是范围示例，不是完整文件清单。新文件也要逐项检查，不要直接把整个本机目录加入提交。

不得发布：

- 私有 `config/config.yaml`、`.env`、应用密钥、Cookie、浏览器 Profile 或认证存储；
- 本机日志、运行账本、媒体、截图、业务提示词、诊断记录和备份；
- `.venv`、浏览器二进制、模型权重、缓存、个人代理配置或机器专用 Compose 覆盖文件。

发布前执行适用的 Python 与 JavaScript 回归。依赖已准备好时：

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
for file in tests/*.cjs; do node "$file" || exit 1; done
```

DOM fixture 测试使用本地合成页面，不应访问真实账号或提交生成任务。运行这些测试仍需安装测试所用的 Playwright 浏览器、FFmpeg 与 FFprobe。

Git 记录 Mac 入口执行位；提交时确认 `.command` 和 `.sh` 的 mode，不能依靠 `.gitattributes` 设置可执行位：

```sh
git ls-files --stage scripts/start_macos.command scripts/start_macos.sh start_mac.command
```

## 提交与推送

在确认目标仓库、权限、测试和暂存内容后，使用普通提交与快进推送。若远端禁止直推，按仓库既有 PR 与检查流程提交，不绕过保护规则。

```sh
git commit -m "Fix macOS startup and request recovery"
git push origin HEAD:main
```

若推送提示远端更新，重新获取并整合，不使用强推覆盖。发布后核对远端提交 SHA、源码文件和自动化检查；“本机测试通过”不能代替远端同步确认。GitHub 凭据应通过现有安全凭据管理工具使用，不写入仓库或命令日志。

## Docker 与 GHCR

Mac 完整浏览器流程使用原生入口，不依赖 Docker。仓库中的 Docker 配置面向 Linux Web/API 预览，不能代替 Mac 本地登录浏览器或桌面能力，详见 [Docker 说明](docker.md)。

GHCR 镜像只有在维护者实际构建、验证并发布后才可供他人使用。私有镜像需要相应权限，删除 `pull_policy` 不会修复 `unauthorized`。不要把源码修复已发布等同于镜像已更新；不要承诺未验证的架构、浏览器自动化或后处理能力。

发布镜像前，应另外检查镜像内容、第三方许可证、平台架构和访问权限。账号令牌通过安全凭据工具或标准输入使用，不放入 Dockerfile、Compose、源码、文档或镜像层中。镜像发布是独立操作，源码提交不会自动授权额外的镜像发布、网站部署或通知。
