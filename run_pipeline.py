"""豆包多账号视频生成自动化 - 流水线入口。

用法:
    python run_pipeline.py all                # 全流程: fetch → scripts → plan → run
    python run_pipeline.py fetch [--force]    # 只拉飞书数据
    python run_pipeline.py scripts [--force] [--limit N]
    python run_pipeline.py plan [--force]     # 切分脚本、生成任务
    python run_pipeline.py run                # 按账号余额分批生成视频（可反复重跑续接）
    python run_pipeline.py status             # 查看任务与账号状态
    python run_pipeline.py inspect-doubao --account doubao1   # 勘察豆包页面选择器
    python run_pipeline.py inspect-gemini                     # 勘察 Gemini 页面选择器
    python run_pipeline.py concat --script s0_1               # 手动拼接某脚本的分段视频
"""
import argparse
import sys

if sys.platform == 'darwin':
    sys.dont_write_bytecode = True

try:  # Windows 控制台 UTF-8 + 行缓冲（后台运行时日志实时可见）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

from doubao_automation.browser_pool import BrowserSession
from doubao_automation.config import get, load_config
from doubao_automation.doubao_agent import DoubaoAgent
from doubao_automation.gemini_web import GeminiWeb
from doubao_automation.orchestrator import Pipeline
from doubao_automation.paths import VIDEOS_DIR, WORKSPACE, ensure_dirs, use_workspace_temp
from doubao_automation.video_utils import concat_videos, find_ffmpeg


def cmd_pipeline(args, cfg):
    p = Pipeline(cfg)
    if args.stage == "all":
        p.run_all(force=args.force, from_saved=args.from_saved, limit=args.limit or 0)
    elif args.stage == "fetch":
        p.stage_fetch(force=args.force)
    elif args.stage == "scripts":
        p.stage_scripts(force=args.force, limit=args.limit or 0)
    elif args.stage == "plan":
        p.stage_plan(force=args.force)
    elif args.stage == "run":
        p.stage_run()
    elif args.stage == "status":
        p.report()


def cmd_inspect_doubao(args, cfg):
    ensure_dirs()
    accts = {a["name"]: a for a in (get(cfg, "accounts.doubao") or [])}
    name = args.account or next(iter(accts), "")
    if name not in accts:
        print(f"未知账号 {name}，可选: {', '.join(accts)}")
        return 2
    acct = accts[name]
    out = WORKSPACE / "inspect"
    session = BrowserSession(name, acct["profile"], cfg,
                             start_url=get(cfg, "doubao.chat_url", "https://www.doubao.com/chat/"))
    session.start()
    try:
        agent = DoubaoAgent(session, cfg, name)
        if args.chat_url:
            from urllib.parse import urlsplit
            if urlsplit(args.chat_url).hostname not in ('www.doubao.com','doubao.com'):
                raise ValueError('只能勘察豆包原对话地址')
            session.page.goto(args.chat_url,wait_until='domcontentloaded')
            session.page.wait_for_timeout(4000)
        if args.video_mode:
            if not args.chat_url:
                agent.open_chat()
                agent.new_chat()
            try:agent.configure_video_mode(inspect_menu=args.duration_menu)
            except Exception:
                agent.inspect_dump(out)
                raise
        agent.inspect_dump(out)
        if args.interactive:
            print("[inspect] 浏览器保持打开：你可以手动点开视频生成入口/发一条消息，然后回到这里按回车再次抓取 DOM…")
            input()
            agent.inspect_dump(out)
        print(f"[inspect] 完成。请把 {out} 下的 *_dom.txt 和截图发给我用于校准选择器。")
    finally:
        session.stop()
    return 0


def cmd_inspect_gemini(args, cfg):
    ensure_dirs()
    g = get(cfg, "accounts.gemini") or {}
    out = WORKSPACE / "inspect"
    session = BrowserSession(g.get("name", "gemini"), g.get("profile", "profiles/gemini"), cfg,
                             start_url=get(cfg, "login.gemini_url", "https://gemini.google.com/app"))
    session.start()
    try:
        GeminiWeb(session, cfg).inspect_dump(out)
        print(f"[inspect] 完成。请把 {out} 下的 gemini_dom.txt 和截图发给我用于校准选择器。")
    finally:
        session.stop()
    return 0


def _make_test_media(kind: str, out_path, cfg):
    """用 ffmpeg 生成测试图片/视频（dry-run 用，不消耗任何生成额度）。"""
    import subprocess
    import sys as _sys
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ff = find_ffmpeg(get(cfg, "video.ffmpeg", "auto"))
    flags = 0x08000000 if _sys.platform == "win32" else 0
    if kind == "image":
        cmd = [ff, "-y", "-loglevel", "error", "-f", "lavfi",
               "-i", "color=c=0x2E86DE:s=640x480:d=1", "-frames:v", "1", str(out_path)]
    else:
        cmd = [ff, "-y", "-loglevel", "error", "-f", "lavfi",
               "-i", "color=c=0x1ABC9C:s=640x480:d=3", "-f", "lavfi",
               "-i", "sine=frequency=440:duration=3", "-shortest",
               "-pix_fmt", "yuv420p", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
    if r.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"生成测试{kind}失败: {(r.stderr or '')[:300]}")
    return out_path


def cmd_dryrun_doubao(args, cfg):
    """上传测试图 + 输入文字但【不发送】→ 抓取发送按钮/缩略图等真实选择器。零额度消耗。"""
    accts = {a["name"]: a for a in (get(cfg, "accounts.doubao") or [])}
    name = args.account or next(iter(accts), "")
    if name not in accts:
        print(f"未知账号 {name}，可选: {', '.join(accts)}")
        return 2
    acct = accts[name]
    out = WORKSPACE / "inspect"
    img = _make_test_media("image", out / "test_image.png", cfg)
    session = BrowserSession(name, acct["profile"], cfg,
                             start_url=get(cfg, "doubao.chat_url", "https://www.doubao.com/chat/"))
    session.start()
    try:
        agent = DoubaoAgent(session, cfg, name)
        agent.open_chat()
        agent.new_chat()
        agent.upload_images([img])
        agent.type_text("【自动化联调测试】此消息不会发送，请忽略")
        session.page.wait_for_timeout(3000)
        agent.inspect_dump(out)
        print("[dryrun] 完成：未发送任何消息，不消耗生成额度。")
    finally:
        session.stop()
    return 0


def cmd_dryrun_gemini(args, cfg):
    """上传测试视频 + 输入文字但【不发送】→ 验证上传链路并抓取真实选择器。零消耗。"""
    g = get(cfg, "accounts.gemini") or {}
    out = WORKSPACE / "inspect"
    vid = _make_test_media("video", out / "test_video.mp4", cfg)
    session = BrowserSession(g.get("name", "gemini"), g.get("profile", "profiles/gemini"), cfg,
                             start_url=get(cfg, "login.gemini_url", "https://gemini.google.com/app"))
    session.start()
    try:
        gw = GeminiWeb(session, cfg)
        gw.open()
        gw.upload_files([vid])
        gw.type_prompt("AUTOMATION TEST - DO NOT SEND")
        session.page.wait_for_timeout(3000)
        gw.inspect_dump(out)
        print("[dryrun] 完成：未发送任何消息。")
    finally:
        session.stop()
    return 0


def cmd_concat(args, cfg):
    snap_ids = args.script
    segs = sorted(VIDEOS_DIR.glob(f"{snap_ids}_seg*.mp4"))
    if len(segs) < 2:
        print(f"找到 {len(segs)} 个分段（{snap_ids}_seg*.mp4），不足以拼接")
        return 2
    out = VIDEOS_DIR / f"{snap_ids}_final.mp4"
    concat_videos(segs, out, find_ffmpeg(get(cfg, "video.ffmpeg", "auto")))
    print(f"已拼接: {out}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="豆包多账号视频生成自动化流水线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for stage in ("all", "fetch", "scripts", "plan", "run", "status"):
        sp = sub.add_parser(stage)
        sp.add_argument("--force", action="store_true")
        sp.add_argument("--from-saved", action="store_true", help="使用面板已保存素材，不重新同步飞书")
        sp.add_argument("--limit", type=int, default=0, help="scripts 阶段最多处理前 N 条源记录")
        sp.set_defaults(func=cmd_pipeline, stage=stage)

    sp = sub.add_parser("inspect-doubao")
    sp.add_argument("--account", default="")
    sp.add_argument("--interactive", action="store_true", help="保持打开，手动操作后再抓一次 DOM")
    sp.add_argument("--video-mode", action="store_true", help="仅打开视频生成控件勘察，不发送不扣额度")
    sp.add_argument("--duration-menu", action="store_true", help="勘察时只展开视频时长菜单")
    sp.add_argument("--chat-url", default="", help="只读勘察已有豆包对话，不重发请求")
    sp.set_defaults(func=cmd_inspect_doubao)

    sp = sub.add_parser("inspect-gemini")
    sp.set_defaults(func=cmd_inspect_gemini)

    sp = sub.add_parser("dryrun-doubao", help="上传测试图+输入但不发送，零额度校准选择器")
    sp.add_argument("--account", default="")
    sp.set_defaults(func=cmd_dryrun_doubao)

    sp = sub.add_parser("dryrun-gemini", help="上传测试视频+输入但不发送，校准上传链路")
    sp.set_defaults(func=cmd_dryrun_gemini)

    sp = sub.add_parser("concat")
    sp.add_argument("--script", required=True, help="脚本 ID，如 s0_1")
    sp.set_defaults(func=cmd_concat)

    args = ap.parse_args()
    cfg = load_config()
    ensure_dirs()
    use_workspace_temp()
    if args.cmd in ("all", "fetch", "scripts", "plan", "run", "status"):
        args.stage = args.cmd
        return cmd_pipeline(args, cfg) or 0
    return args.func(args, cfg) or 0


if __name__ == "__main__":
    sys.exit(main())
