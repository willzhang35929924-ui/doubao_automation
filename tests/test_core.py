"""核心逻辑自检（零第三方依赖，只需 Python 本体）。

环境恢复后第一件事运行:  python tests\\test_core.py
验证：时长估算、脚本切分、Gemini 回复解析、口播抽取、首尾帧衔接、
状态持久化、账号池领取/耗尽/跨天恢复。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from doubao_automation.account_manager import AVAILABLE, AccountManager  # noqa: E402
from doubao_automation.script_splitter import (  # noqa: E402
    build_segments, chunk_narration, count_spoken,
    estimate_seconds, extract_narration, parse_scripts,
)
from doubao_automation.state import JsonStore  # noqa: E402

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ✓ {name}")


CFG = {"video": {"chars_per_second": 4.5, "target_segment_seconds": 13}}
MAX_CHARS = int(13 * 4.5)  # 58
SENT_END = "。！？!?；;…"

print("== 1. 时长估算 ==")
check("45 口播字 ≈ 10 秒", abs(estimate_seconds("字" * 45, 4.5) - 10.0) < 0.11)
check("标点不计入", count_spoken("你好，世界！") == 4)

print("== 2. 文案切分 ==")
long_text = "".join(f"这是第{i}句产品卖点介绍。" for i in range(1, 19))  # 198 口播字 ≈ 44 秒
chunks = chunk_narration(long_text, MAX_CHARS)
check("44 秒文案切成多段", len(chunks) >= 3)
check("每段不超限", all(count_spoken(c) <= MAX_CHARS for c in chunks))
check("口播字无丢失", sum(count_spoken(c) for c in chunks) == count_spoken(long_text))
check("每段以句读结尾", all(c[-1] in SENT_END for c in chunks))
hard = "无标点超长文案" * 30
hc = chunk_narration(hard, MAX_CHARS)
check("无标点长文硬切且不超限", len(hc) >= 4 and all(count_spoken(c) <= MAX_CHARS for c in hc))
tiny = ("短句！" + "甲乙丙丁戊己庚辛壬癸空亢艮合室张索笙商，" + "太尊贤根林列载昭激收，" + "申酉戌亥天地人和速束亢角键征意符。")
tc = chunk_narration(tiny, MAX_CHARS, min_spoken=22)
check("短段平衡：无低于下限的段", all(count_spoken(c) >= 22 for c in tc) or len(tc) == 1)
check("短段平衡：口播字无丢失", sum(count_spoken(c) for c in tc) == count_spoken(tiny))

print("== 3. Gemini 回复解析 ==")
gemini_out = """好的，以下是生成的三个脚本：

### 脚本1
- 卖点定位：便携
- 口播文案：还在为充电慢发愁吗？这款充电宝二十分钟充到一半！
- 画面风格：明亮桌面

### 脚本2
- 卖点定位：轻薄
- 口播文案：只有手机一半厚，塞进口袋毫无压力，出门旅行必备好物。
- 画面风格：户外阳光

### 脚本3
- 卖点定位：安全
- 口播文案：多重保护芯片，发热自动断电，用得安心才是好电源。
- 画面风格：特写镜头
"""
scripts = parse_scripts(gemini_out)
check("解析出 3 个脚本", len(scripts) == 3)
check("每个脚本含口播文案", all("口播文案" in s for s in scripts))
check("标题行不泄漏进正文", all("### 脚本" not in s for s in scripts))
bold = "前言\n**脚本1**\n文案一\n**脚本2：加强版**\n文案二"
check("加粗标记也能解析", len(parse_scripts(bold)) == 2)
check("无标记时整体作为单脚本", parse_scripts("只有一段整体内容") == ["只有一段整体内容"])
bare = "Gemini said\n脚本1\n卖点：a\n口播文案：文案一内容。\n脚本2\n口播文案：文案二内容。\n"
bs = parse_scripts(bare)
check("裸标题+Gemini said 前缀解析", len(bs) == 2 and "Gemini said" not in bs[0])

print("== 4. 口播文案抽取 ==")
nar = extract_narration(scripts[0])
check("冒号后内联抽取", nar == "还在为充电慢发愁吗？这款充电宝二十分钟充到一半！")
s_colon_empty = "- 卖点定位：x\n- 口播文案：\n第一行文案。\n第二行文案。\n- 画面风格：y"
nar2 = extract_narration(s_colon_empty)
check("冒号为空时取后续行", "第一行文案。" in nar2 and "第二行文案。" in nar2 and "画面风格" not in nar2)
s_plain = "### 脚本9\n- 卖点定位：防水\n- 画面风格：雨中\n直接开场的口播句子，没有任何标签。"
nar3 = extract_narration(s_plain)
check("无标签时回退全文并剔除要点行", "直接开场的口播句子" in nar3 and "卖点定位" not in nar3)

print("== 5. 分段与首尾帧衔接 ==")
long_script = "- 口播文案：" + long_text + "\n- 画面风格：电商风"
segs = build_segments(long_script, CFG, product_name="测试充电宝")
check("生成多个分段", len(segs) >= 3)
check("段索引连续", [s["index"] for s in segs] == list(range(len(segs))))
check("每段预计时长 ≤13.5s", all(s["est_seconds"] <= 13.5 for s in segs))
chain_ok = all(segs[i]["start_frame_desc"] == segs[i - 1]["end_frame_desc"] for i in range(1, len(segs)))
check("首帧描述 == 上一段尾帧描述（链式一致）", chain_ok)
check("提示词含首帧/口播/尾帧要素",
      all(("首帧画面" in s["prompt"] and "口播文案" in s["prompt"] and "尾帧画面" in s["prompt"]) for s in segs))
check("后续段声明以上传图为首帧", "以它作为本段视频的首帧" in segs[1]["prompt"])
check("产品名注入提示词", "测试充电宝" in segs[0]["prompt"])
check("分段文案拼回≈原文", sum(count_spoken(s["narration"]) for s in segs) == count_spoken(long_text))

print("== 6. 状态持久化 ==")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "s.json"
    st = JsonStore(p, initial={"tasks": []})
    with st.txn() as d:
        d["tasks"].append({"id": "t1", "status": "pending"})
    st2 = JsonStore(p)
    check("JsonStore 落盘回读", st2.snapshot()["tasks"][0]["id"] == "t1")

print("== 6.5 时间戳脚本 ==")
TS_SCRIPT = """Script 01：测试标题
场景设定：桌面。
时间戳分解
00:00 - 00:05｜摄影与构图：特写。
视觉与动作：手捏产品。
照明与氛围：暖光。
音频与口播：(欢快) "Hello!"
00:05 - 00:15｜摄影与构图：中景。
视觉与动作：展示产品全貌。
照明与氛围：自然光。
音频与口播：(温柔) "Buy now!"
Script 02：第二个
场景设定：户外。
时间戳分解
00:00 - 00:15｜摄影与构图：跟拍。
视觉与动作：走动展示。
照明与氛围：阳光。
音频与口播：(清爽) "Great!"
"""
tsc = parse_scripts(TS_SCRIPT)
check("Script NN 标题解析", len(tsc) == 2)
seg15 = build_segments(tsc[0], CFG, product_name="P")
check("15秒时间戳脚本=1段", len(seg15) == 1 and seg15[0]["est_seconds"] == 15)
check("时间戳段提示词含原始块", "00:00 - 00:05" in seg15[0]["prompt"] and "场景设定" in seg15[0]["prompt"])
TS40 = (TS_SCRIPT.split("Script 02")[0]
        + "00:15 - 00:30｜摄影与构图：特写。\n视觉与动作：细节。\n照明与氛围：暖。\n音频与口播：\"More!\"\n"
        + "00:30 - 00:40｜摄影与构图：拉远。\n视觉与动作：收尾。\n照明与氛围：明亮。\n音频与口播：\"Bye!\"\n")
seg40 = build_segments(TS40, CFG, product_name="P")
check("40秒时间戳脚本按块边界切3段", len(seg40) == 3)
check("时间戳分段时长≤15s", all(s["est_seconds"] <= 15 for s in seg40))
check("时间戳分段首尾帧链", all(seg40[i]["start_frame_desc"] == seg40[i - 1]["end_frame_desc"]
                              for i in range(1, len(seg40))))

print("== 7. 账号池 ==")
with tempfile.TemporaryDirectory() as td:
    st = JsonStore(Path(td) / "a.json")
    am = AccountManager(st, ["doubao1", "doubao2", "doubao3"])
    check("顺序领取 doubao1", am.pick() == "doubao1")
    check("忙碌时领取 doubao2", am.pick() == "doubao2")
    am.release("doubao1")
    check("释放后优先复用 doubao1", am.pick() == "doubao1")
    am.mark_exhausted("doubao1")
    am.release("doubao2")
    check("跳过量尽账号", am.pick() == "doubao2")
    am.mark_exhausted("doubao2")
    am.release("doubao3")
    got = am.pick()
    check("领取最后一个可用账号", got == "doubao3")
    check("全部忙碌/耗尽时无账号可领", am.pick() == "")
    with st.txn() as d:  # 模拟昨天耗尽
        for acc in d["accounts"].values():
            if acc.get("date"):
                acc["date"] = "2000-01-01"
    am.reset_daily()
    check("跨天额度自动恢复",
          am.status("doubao1")["status"] == AVAILABLE and am.status("doubao2")["status"] == AVAILABLE)
    am.mark_needs_login("doubao3")
    check("需登录状态标记", am.status("doubao3")["status"] == "needs_login")

print(f"\n全部 {PASS} 项自检通过 ✅")
