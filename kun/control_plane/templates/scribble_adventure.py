"""Original word-to-world adventure template.

This template targets functional parity with an open-ended word-summoning
adventure while avoiding protected commercial expression.  It intentionally
uses original names, worlds, UI copy, package identifiers, and delivery docs.
"""

from __future__ import annotations

from kun.control_plane.templates.huohutu_scribble_parity import scribble_parity_ready_files


def scribble_adventure_ready_files() -> dict[str, str]:
    """Return an original, non-cloning word-to-world adventure template."""

    files = scribble_parity_ready_files()
    return {path: _rewrite_content(path, content) for path, content in files.items()}


def _rewrite_content(path: str, content: str) -> str:
    replacements = [
        ("huohutu-scribble-functional-parity", "wordforge-adventure-functional-parity"),
        ("HuohutuSpark", "WordforgeAdventure"),
        ("com.huohutu.spark", "com.wordforge.adventure"),
        ("火火兔 Scribble Spark Parity", "文字造物冒险"),
        ("Scribble Spark", "Wordforge Adventure"),
        ("火火兔 Scribble Spark Functional Parity V5", "文字造物冒险 Functional Parity V1"),
        ("Fire Rabbit Scribble Spark System Design", "Wordforge Adventure System Design"),
        ("Fire Rabbit expression", "original Wordforge expression"),
        ("原创火火兔表达", "原创文字造物表达"),
        ("火火兔独有", "文字造物独有"),
        ("火火兔 Spark", "文字造物冒险"),
        ("火火兔", "文字造物"),
        ("huohutu-scribble-parity-v5", "wordforge-adventure-parity-v1"),
        ("彩虹造物岛", "词语造物岛"),
        ("造物岛", "造物岛"),
        ("故事星球", "谜题故事星"),
        ("故事星", "故事星"),
        ("齿轮花园", "机关花园"),
        ("齿轮园", "机关园"),
        ("云端港湾", "浮空港湾"),
        ("云港", "浮空港"),
        ("小火", "小笔"),
        ("泡泡船长", "句号船长"),
        ("咔哒熊", "齿轮向导"),
        ("云朵猫", "云上向导"),
        ("Spark traces", "Idea traces"),
        ("火花解读", "灵感解读"),
        ("火花碎片", "灵感碎片"),
        ("火花", "灵感"),
        ("表达火花", "表达灵感"),
        ("创造火花", "创造灵感"),
        ("故事火花", "故事灵感"),
        ("探索火花", "探索灵感"),
        ("共情火花", "共情灵感"),
        ("自然火花", "自然灵感"),
        ("AI协作火花", "AI协作灵感"),
        ("家长火花册", "玩家日志册"),
        ("parent firebook", "player logbook"),
        ("parent_firebook", "player_logbook"),
        ("Fire Rabbit", "Wordforge"),
        ("fire_rabbit", "wordforge"),
        ("huohutu", "wordforge"),
        ("Scribble", "Wordforge"),
    ]
    rewritten = content
    for old, new in replacements:
        rewritten = rewritten.replace(old, new)
    if path == "README.md":
        rewritten += (
            "\n## 原创边界\n\n"
            "- 本项目只对标开放式文字造物、形容词属性、对象组合、NPC 请求、奖励循环和关卡沙盒等系统能力。\n"
            "- 不复制商业游戏的角色、美术、关卡、文案、音频、商标、UI 皮肤或 trade dress。\n"
        )
    return rewritten
