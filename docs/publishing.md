# 手动学习发布流程（本次未执行）

本发布副本位于原项目目录内部。当前它没有独立 `.git`，从这里执行 Git 命令可能会操作**外层原项目仓库**。以下仅供用户学习和人工执行；发布前应先完成隐私审计、测试和第三方许可证检查。不要在未确认仓库根目录时执行 `git add` 或提交。

## GitHub

可使用仓库名 `doubao-automation`。确认当前文件夹是发布副本后，建立独立仓库并检查根目录：

```sh
git rev-parse --show-toplevel
# 以上若显示外层原项目，说明还不能在该仓库执行 add。
git init
git rev-parse --show-toplevel
git status --short --untracked-files=all
git add --dry-run .
```

确认第二次 `rev-parse` 指向发布副本，检查待添加文件不包含私有配置、账号、日志、素材、模型、二进制和缓存，并审查新增代码后，才可由使用者自行继续：

```sh
git add .
git diff --cached --stat
git diff --cached --check
git commit -m "Prepare initial source release"
git branch -M main
git remote add origin https://github.com/OWNER/doubao-automation.git
git push -u origin main
```

将 `OWNER` 换成自己的账号或组织。macOS 的 `.command` 和 `.sh` 文件还需在提交前确认 Git 可执行位；例如逐个执行 `git update-index --chmod=+x scripts/start_macos.command`。这些都是 Git 写操作，本次未执行。

## GHCR

镜像命名示例：`ghcr.io/OWNER/doubao-automation:preview`。该镜像仅为尚未构建或验证的 Linux Web/API 预览，不含浏览器与后处理依赖。登录时使用有相应权限的个人凭据，通过标准输入传入；不要把凭据写进源码、文档或镜像：

```sh
printf '%s' "$CR_PAT" | docker login ghcr.io -u OWNER --password-stdin
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/OWNER/doubao-automation:preview --push .
docker pull ghcr.io/OWNER/doubao-automation:preview
```

`--push` 会实际推送镜像；不要在确认凭据、镜像内容和目标仓库之前执行。若仅针对当前架构使用普通 `docker build`，构建后的镜像可以用 `docker push ghcr.io/OWNER/doubao-automation:preview` 推送。有关主机网络和容器限制参见 [Docker 说明](docker.md)。
