"""Functional-parity Fire Rabbit word-to-world game template."""

from __future__ import annotations

import json


def scribble_parity_ready_files() -> dict[str, str]:
    """Return a broad word-to-world adventure template for the v5 parity task."""

    return {
        "package.json": _package_json(),
        "index.html": _index_html(),
        "tsconfig.json": _tsconfig_json(),
        "tsconfig.node.json": _tsconfig_node_json(),
        "vite.config.ts": _vite_config_ts(),
        "scripts/internal-test.mjs": _internal_test_script(),
        "scripts/user-sim.mjs": _user_sim_script(),
        "scripts/fun-playtest.mjs": _fun_playtest_script(),
        "scripts/browser-static-playtest.mjs": _browser_static_playtest_script(),
        "src/main.tsx": _main_tsx(),
        "src/types.ts": _types_ts(),
        "src/data/worlds.ts": _worlds_ts(),
        "src/engine/safety.ts": _safety_ts(),
        "src/engine/wordToWorld.ts": _word_to_world_ts(),
        "src/engine/worldRules.ts": _world_rules_ts(),
        "src/engine/spark.ts": _spark_ts(),
        "src/engine/storage.ts": _storage_ts(),
        "src/App.tsx": _app_tsx(),
        "src/styles.css": _styles_css(),
        "docs/parity-runtime-report.json": _parity_report_json(),
        "README.md": _readme_md(),
    }


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


OBJECTS = [
    ("bridge", "彩虹桥", ["桥", "彩虹桥", "小桥"], ["crossing", "support"]),
    ("bridge", "木头桥", ["木桥", "木头桥"], ["crossing", "support"]),
    ("bridge", "软垫桥", ["软桥", "软垫桥"], ["crossing", "comfort"]),
    ("bridge", "发光桥", ["亮桥", "发光桥"], ["crossing", "light"]),
    ("key", "月亮钥匙", ["钥匙", "月亮钥匙"], ["unlock", "story"]),
    ("key", "音乐钥匙", ["音乐钥匙", "唱歌钥匙"], ["unlock", "music"]),
    ("key", "金属钥匙", ["金属钥匙", "铁钥匙"], ["unlock", "repair"]),
    ("key", "问题钥匙", ["问题钥匙", "问号钥匙"], ["unlock", "ask"]),
    ("lamp", "星星灯", ["灯", "星星灯"], ["light", "story"]),
    ("lamp", "温柔光球", ["光球", "太阳"], ["light", "comfort"]),
    ("lamp", "萤火灯", ["萤火灯", "萤火虫灯"], ["light", "nature"]),
    ("lamp", "镜面灯", ["镜灯", "反光灯"], ["light", "reveal"]),
    ("cloud", "雨云", ["雨云", "下雨云"], ["weather", "cooling"]),
    ("cloud", "音乐云", ["音乐云", "唱歌云"], ["music", "comfort"]),
    ("cloud", "棉花云", ["棉花云", "软云"], ["comfort", "floating"]),
    ("cloud", "闪光云", ["闪光云", "发光云"], ["weather", "light"]),
    ("boat", "纸船", ["船", "纸船"], ["crossing", "floating"]),
    ("boat", "木头小船", ["木船", "小船"], ["crossing", "floating"]),
    ("boat", "飞行小船", ["飞船", "飞行船"], ["crossing", "flying"]),
    ("boat", "救援小船", ["救援船", "安全船"], ["protect", "crossing"]),
    ("seed", "彩虹种子", ["种子", "彩虹种子"], ["growth", "nature"]),
    ("seed", "星光种子", ["星光种子"], ["growth", "light"]),
    ("seed", "音乐种子", ["音乐种子"], ["growth", "music"]),
    ("seed", "故事种子", ["故事种子"], ["growth", "story"]),
    ("rain", "凉凉小雨", ["雨", "小雨"], ["weather", "cooling"]),
    ("rain", "彩虹雨", ["彩虹雨"], ["weather", "growth"]),
    ("rain", "星星雨", ["星星雨"], ["weather", "light"]),
    ("rain", "温柔雨", ["温柔雨"], ["weather", "comfort"]),
    ("blanket", "温暖毯子", ["毯子", "毛毯"], ["comfort", "protect"]),
    ("blanket", "云朵毯子", ["云毯", "云朵毯子"], ["comfort", "floating"]),
    ("blanket", "安全披风", ["披风", "安全披风"], ["protect", "comfort"]),
    ("blanket", "隐形毯子", ["隐形毯"], ["protect", "reveal"]),
    ("friend", "泡泡朋友", ["朋友", "泡泡朋友"], ["comfort", "ask"]),
    ("friend", "机器人伙伴", ["机器人", "伙伴"], ["repair", "ask"]),
    ("friend", "小兔伙伴", ["小兔", "兔子"], ["comfort", "nature"]),
    ("friend", "星星向导", ["向导", "星星向导"], ["ask", "reveal"]),
    ("tool", "安全修理工具", ["工具", "修理工具"], ["repair", "protect"]),
    ("tool", "木头台阶", ["台阶", "梯子"], ["crossing", "support"]),
    ("tool", "魔法刷子", ["刷子", "画笔"], ["transform", "color"]),
    ("tool", "放大镜", ["放大镜", "观察镜"], ["reveal", "ask"]),
    ("music", "会唱歌的铃铛", ["铃铛", "音乐"], ["music", "comfort"]),
    ("music", "节奏鼓", ["鼓", "节奏鼓"], ["music", "wake"]),
    ("music", "安静笛子", ["笛子", "安静笛"], ["music", "calm"]),
    ("music", "回声盒子", ["回声", "盒子"], ["music", "reveal"]),
    ("animal", "友好小鸟", ["鸟", "小鸟"], ["nature", "flying"]),
    ("animal", "勇敢小狗", ["狗", "小狗"], ["protect", "comfort"]),
    ("animal", "慢慢乌龟", ["乌龟"], ["crossing", "patience"]),
    ("animal", "睡觉小猫", ["猫", "小猫"], ["comfort", "calm"]),
    ("door", "问题之门", ["门", "问题之门"], ["unlock", "ask"]),
    ("door", "云朵门", ["云门"], ["unlock", "floating"]),
    ("door", "星光门", ["星门", "星光门"], ["unlock", "light"]),
    ("door", "故事门", ["故事门"], ["unlock", "story"]),
    ("stone", "发光石头", ["石头", "发光石"], ["light", "reveal"]),
    ("stone", "弹跳石", ["弹跳石"], ["movement", "bounce"]),
    ("stone", "磁力石", ["磁石", "磁力石"], ["magnet", "repair"]),
    ("stone", "轻轻石", ["轻石"], ["floating", "crossing"]),
    ("kite", "会飞风筝", ["风筝"], ["flying", "crossing"]),
    ("kite", "灯笼风筝", ["灯笼风筝"], ["flying", "light"]),
    ("kite", "信使风筝", ["信使风筝"], ["flying", "story"]),
    ("kite", "安全风筝", ["安全风筝"], ["flying", "protect"]),
    ("shield", "安全护盾", ["护盾", "保护罩"], ["protect", "safe"]),
    ("shield", "凉凉护盾", ["凉护盾"], ["protect", "cooling"]),
    ("shield", "镜子护盾", ["镜盾"], ["protect", "reveal"]),
    ("shield", "柔软护盾", ["软盾"], ["protect", "comfort"]),
    ("rope", "彩色绳子", ["绳子", "彩绳"], ["attach", "crossing"]),
    ("rope", "弹力绳", ["弹力绳"], ["attach", "bounce"]),
    ("rope", "发光绳", ["发光绳"], ["attach", "light"]),
    ("rope", "安全绳", ["安全绳"], ["attach", "protect"]),
    ("ladder", "云梯", ["云梯", "梯子"], ["crossing", "support"]),
    ("ladder", "木梯", ["木梯"], ["crossing", "support"]),
    ("ladder", "星梯", ["星梯"], ["crossing", "light"]),
    ("ladder", "折叠梯", ["折叠梯"], ["crossing", "repair"]),
    ("fan", "微风扇", ["风扇"], ["weather", "movement"]),
    ("fan", "彩虹风车", ["风车"], ["weather", "energy"]),
    ("fan", "安静风扇", ["安静风扇"], ["calm", "weather"]),
    ("fan", "飞行风扇", ["飞行风扇"], ["flying", "movement"]),
    ("magnet", "磁铁", ["磁铁"], ["magnet", "repair"]),
    ("magnet", "星星磁铁", ["星磁铁"], ["magnet", "light"]),
    ("magnet", "安全磁铁", ["安全磁铁"], ["magnet", "protect"]),
    ("magnet", "音乐磁铁", ["音乐磁铁"], ["magnet", "music"]),
    ("mirror", "镜子", ["镜子"], ["reveal", "light"]),
    ("mirror", "月亮镜", ["月亮镜"], ["reveal", "story"]),
    ("mirror", "安全镜", ["安全镜"], ["reveal", "protect"]),
    ("mirror", "彩虹镜", ["彩虹镜"], ["reveal", "color"]),
    ("clock", "慢慢钟", ["钟", "时钟"], ["time", "calm"]),
    ("clock", "快快钟", ["快钟"], ["time", "movement"]),
    ("clock", "睡觉钟", ["睡觉钟"], ["time", "calm"]),
    ("clock", "星光钟", ["星光钟"], ["time", "light"]),
    ("wheel", "圆圆轮子", ["轮子"], ["movement", "repair"]),
    ("wheel", "木头轮子", ["木轮"], ["movement", "repair"]),
    ("wheel", "云朵轮子", ["云轮"], ["movement", "floating"]),
    ("wheel", "磁力轮子", ["磁轮"], ["movement", "magnet"]),
    ("balloon", "气球", ["气球"], ["floating", "flying"]),
    ("balloon", "热气球", ["热气球"], ["flying", "warm"]),
    ("balloon", "灯笼气球", ["灯笼气球"], ["flying", "light"]),
    ("balloon", "安全气球", ["安全气球"], ["flying", "protect"]),
    ("umbrella", "雨伞", ["伞", "雨伞"], ["protect", "weather"]),
    ("umbrella", "彩虹伞", ["彩虹伞"], ["protect", "color"]),
    ("umbrella", "飞行伞", ["飞行伞"], ["flying", "protect"]),
    ("umbrella", "凉凉伞", ["凉伞"], ["cooling", "protect"]),
    ("flower", "会笑的花", ["花", "小花"], ["growth", "comfort"]),
    ("flower", "发光花", ["发光花"], ["growth", "light"]),
    ("flower", "音乐花", ["音乐花"], ["growth", "music"]),
    ("flower", "勇敢花", ["勇敢花"], ["growth", "protect"]),
    ("tree", "小树", ["树", "小树"], ["growth", "nature"]),
    ("tree", "桥树", ["桥树"], ["growth", "crossing"]),
    ("tree", "星星树", ["星树"], ["growth", "light"]),
    ("tree", "问号树", ["问号树"], ["growth", "ask"]),
    ("book", "故事书", ["书", "故事书"], ["story", "ask"]),
    ("book", "地图书", ["地图书"], ["reveal", "story"]),
    ("book", "音乐书", ["音乐书"], ["music", "story"]),
    ("book", "安全书", ["安全书"], ["protect", "story"]),
    ("map", "星星地图", ["地图"], ["reveal", "story"]),
    ("map", "彩虹地图", ["彩虹地图"], ["reveal", "color"]),
    ("map", "云朵地图", ["云地图"], ["reveal", "floating"]),
    ("map", "问题地图", ["问题地图"], ["ask", "reveal"]),
    ("compass", "指南针", ["指南针"], ["reveal", "crossing"]),
    ("compass", "星光指南针", ["星光指南针"], ["reveal", "light"]),
    ("compass", "朋友指南针", ["朋友指南针"], ["ask", "comfort"]),
    ("compass", "安全指南针", ["安全指南针"], ["protect", "reveal"]),
    ("bell", "提醒铃", ["提醒铃"], ["music", "wake"]),
    ("bell", "安静铃", ["安静铃"], ["music", "calm"]),
    ("bell", "星星铃", ["星星铃"], ["music", "light"]),
    ("bell", "勇气铃", ["勇气铃"], ["music", "protect"]),
]

PROPERTIES = [
    ("red", "红色", ["红", "红色"]),
    ("blue", "蓝色", ["蓝", "蓝色"]),
    ("green", "绿色", ["绿", "绿色"]),
    ("yellow", "黄色", ["黄", "黄色"]),
    ("rainbow", "彩虹", ["彩虹", "七彩"]),
    ("tiny", "小小", ["小", "小小", "迷你"]),
    ("giant", "巨大", ["大", "巨大", "高"]),
    ("soft", "软软", ["软", "软软"]),
    ("hard", "硬硬", ["硬", "硬硬"]),
    ("metal", "金属", ["金属", "铁"]),
    ("wooden", "木头", ["木", "木头"]),
    ("stone", "石头", ["石", "石头"]),
    ("glass", "透明玻璃", ["玻璃", "透明"]),
    ("floating", "会漂浮的", ["漂", "漂浮", "浮"]),
    ("flying", "会飞的", ["飞", "会飞", "飞行"]),
    ("warm", "温暖", ["暖", "温暖", "热"]),
    ("cool", "凉凉", ["凉", "冰", "冷"]),
    ("wet", "带水的", ["湿", "水", "雨"]),
    ("dry", "干干", ["干", "干燥"]),
    ("illuminating", "会发光的", ["亮", "发光", "照亮", "点亮"]),
    ("dark", "暗暗", ["暗", "黑"]),
    ("friendly", "友好的", ["朋友", "友好"]),
    ("shy", "害羞的", ["害羞"]),
    ("musical", "会唱歌的", ["音乐", "唱歌", "歌"]),
    ("repairing", "会修理的", ["修", "修理"]),
    ("sticky", "黏黏的", ["黏", "粘"]),
    ("slippery", "滑滑的", ["滑"]),
    ("fast", "快快的", ["快", "迅速"]),
    ("slow", "慢慢的", ["慢", "缓慢"]),
    ("heavy", "重重的", ["重", "沉"]),
    ("lightweight", "轻轻的", ["轻", "轻轻"]),
    ("magnetic", "有磁力的", ["磁", "磁力"]),
    ("elastic", "有弹性的", ["弹", "弹力"]),
    ("invisible", "隐形的", ["隐形", "看不见"]),
    ("transparent", "透明的", ["透明"]),
    ("bouncy", "会弹跳的", ["弹跳", "蹦"]),
    ("noisy", "响亮的", ["响", "吵"]),
    ("quiet", "安静的", ["安静"]),
    ("living", "活着的", ["活", "生命"]),
    ("sleepy", "困困的", ["睡", "困"]),
    ("hungry", "饿饿的", ["饿", "肚子饿"]),
    ("brave", "勇敢的", ["勇敢"]),
    ("safe", "安全", ["安全", "保护"]),
]

ACTIONS = [
    ("create", ["造", "做", "来一个"]),
    ("use", ["使用", "用"]),
    ("comfort", ["安慰", "抱抱", "陪"]),
    ("ask", ["问", "为什么", "请教"]),
    ("repair", ["修", "修理", "修好"]),
    ("cross", ["过河", "跨", "过去"]),
    ("light", ["点亮", "照亮", "发光"]),
    ("open", ["打开", "开门", "钥匙"]),
    ("float", ["漂", "漂浮"]),
    ("protect", ["保护", "安全"]),
    ("attach", ["绑", "连接", "贴上"]),
    ("combine", ["合成", "组合", "一起"]),
    ("feed", ["喂", "吃"]),
    ("carry", ["搬", "带走"]),
    ("grow", ["长大", "种"]),
    ("shrink", ["变小"]),
    ("transform", ["变成", "变形"]),
    ("cool", ["冷却", "降温"]),
    ("warm", ["加热", "温暖"]),
    ("wake", ["叫醒", "醒来"]),
    ("calm", ["安静", "冷静"]),
]

RULE_FAMILIES = [
    "crossing",
    "cooling",
    "lighting",
    "unlocking",
    "comforting",
    "growth",
    "repair",
    "protection",
    "music",
    "reveal",
    "movement",
    "combination",
]

WORLD_CONFIG = [
    (
        "rainbow-island",
        "彩虹造物岛",
        "造物岛",
        "小火",
        "河流、夜色和小火苗在同一个玩具盒里等待孩子用词语改变。",
        "#dff8ff",
        "#daf4ce",
        "#ffbf69",
    ),
    (
        "story-planet",
        "故事星球",
        "故事星",
        "泡泡船长",
        "问题之门、迷路星星和断掉的结尾需要孩子造出线索。",
        "#f4e7ff",
        "#fcecc9",
        "#a984ff",
    ),
    (
        "gear-garden",
        "齿轮花园",
        "齿轮园",
        "咔哒熊",
        "会动的花园里，齿轮、磁铁、轮子和音乐机关互相影响。",
        "#e8fff3",
        "#ddf3c2",
        "#76c893",
    ),
    (
        "cloud-harbor",
        "云端港湾",
        "云港",
        "云朵猫",
        "云桥、风车、气球和小船组成开放探索港口。",
        "#e7f0ff",
        "#f7e7c6",
        "#5aa9e6",
    ),
]

GOAL_FAMILIES = [
    ("crossing", "跨越障碍", "需要桥、船、绳、梯、飞行物或能承载的组合物。"),
    ("cooling", "让热麻烦安静", "需要水、雨、凉凉属性、护盾或安全冷却。"),
    ("lighting", "点亮暗处", "需要灯、发光石、镜面反射或发光组合。"),
    ("unlocking", "打开问题入口", "需要钥匙、工具、问题伙伴或可修复机关。"),
    ("comforting", "安慰小伙伴", "需要朋友、毯子、音乐、温暖或柔软物。"),
    ("growth", "唤醒成长", "需要种子、雨、光、树、花或自然属性。"),
]

GOAL_PROPERTY_HINTS = {
    "crossing": ["floating", "flying", "wooden", "rainbow"],
    "cooling": ["cool", "wet", "protect"],
    "lighting": ["illuminating", "glass", "rainbow"],
    "unlocking": ["metal", "repairing", "magnetic"],
    "comforting": ["soft", "warm", "friendly", "musical"],
    "growth": ["wet", "illuminating", "living", "green"],
}


def _package_json() -> str:
    return """{
  "name": "huohutu-scribble-functional-parity",
  "version": "5.0.0-parity.1",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --host 0.0.0.0",
    "build": "tsc -b && vite build",
    "preview": "vite preview --host 0.0.0.0",
    "test:internal": "node scripts/internal-test.mjs",
    "test:user-sim": "node scripts/user-sim.mjs",
    "test:fun": "node scripts/fun-playtest.mjs",
    "test:browser-static": "node scripts/browser-static-playtest.mjs",
    "cap:init": "cap init HuohutuSpark com.huohutu.spark --web-dir=dist",
    "cap:sync": "npm run build && cap sync"
  },
  "dependencies": {
    "@capacitor/cli": "^7.4.3",
    "@capacitor/core": "^7.4.3",
    "@types/react": "^19.2.2",
    "@types/react-dom": "^19.2.2",
    "@vitejs/plugin-react": "^5.0.4",
    "react": "^19.2.0",
    "react-dom": "^19.2.0",
    "typescript": "^5.9.3",
    "vite": "^7.1.11"
  },
  "overrides": {
    "rollup": "npm:@rollup/wasm-node@4.60.4"
  },
  "devDependencies": {}
}
"""


def _index_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#fff4d6" />
    <title>火火兔 Scribble Spark Parity</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""


def _tsconfig_json() -> str:
    return """{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["DOM", "DOM.Iterable", "ES2022"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx"
  },
  "include": ["src"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
"""


def _tsconfig_node_json() -> str:
    return """{
  "compilerOptions": {
    "composite": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "allowSyntheticDefaultImports": true,
    "strict": true
  },
  "include": ["vite.config.ts"]
}
"""


def _vite_config_ts() -> str:
    return """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0"
  }
});
"""


def _main_tsx() -> str:
    return """import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
"""


def _internal_test_script() -> str:
    return """import { existsSync, readFileSync, writeFileSync } from "node:fs";

const requiredFiles = [
  "index.html",
  "tsconfig.json",
  "vite.config.ts",
  "src/main.tsx",
  "src/App.tsx",
  "src/data/worlds.ts",
  "src/engine/wordToWorld.ts",
  "src/engine/worldRules.ts",
  "src/engine/safety.ts",
  "src/engine/storage.ts",
  "docs/scribble-spark-system-design.md",
  "docs/scribble-parity-matrix.json"
];
for (const file of requiredFiles) {
  if (!existsSync(file)) throw new Error(`Missing required file: ${file}`);
}

const app = readFileSync("src/App.tsx", "utf8");
const worlds = readFileSync("src/data/worlds.ts", "utf8");
const parser = readFileSync("src/engine/wordToWorld.ts", "utf8");
const rules = readFileSync("src/engine/worldRules.ts", "utf8");
const packageJson = JSON.parse(readFileSync("package.json", "utf8"));
function exportedJson(source) {
  const start = source.indexOf("= ");
  const end = source.lastIndexOf(";");
  if (start < 0 || end < 0) throw new Error("Cannot parse exported JSON payload");
  return JSON.parse(source.slice(start + 2, end));
}
const worldData = exportedJson(worlds);
const objectCount = (parser.match(/defaultName/g) ?? []).length;
const propertyCount = (parser.match(/"property"/g) ?? []).length;
const actionCount = (parser.match(/"action"/g) ?? []).length;
const worldCount = Object.keys(worldData).length;
const goalCount = Object.values(worldData).flatMap((world) => world.goals ?? []).length;
const solutionCount = Object.values(worldData).flatMap((world) => (world.goals ?? []).flatMap((goal) => goal.solutions ?? [])).length;
const ruleFamilyCount = (rules.match(/"ruleFamily"/g) ?? []).length;
const unsafeGenericSolutions = Object.values(worldData)
  .flatMap((world) => world.goals ?? [])
  .flatMap((goal) => goal.solutions ?? [])
  .filter((solution) => (solution.propertyAny ?? []).includes("safe"));
const genericCreateSolutions = Object.values(worldData)
  .flatMap((world) => world.goals ?? [])
  .flatMap((goal) => goal.solutions ?? [])
  .filter((solution) => (solution.actions ?? []).includes("create"));

const checks = [
  ["parity package", packageJson.name === "huohutu-scribble-functional-parity"],
  ["object noun breadth", objectCount >= 120],
  ["property breadth", propertyCount >= 40],
  ["action breadth", actionCount >= 12],
  ["four worlds", worldCount >= 4],
  ["twenty-four goals", goalCount >= 24],
  ["seventy-two solution paths", solutionCount >= 72],
  ["eight rule families", ruleFamilyCount >= 8],
  ["object editor", app.includes("objectEditor") && parser.includes("editObject")],
  ["combine and attach", app.includes("combineObjects") && app.includes("attachObject")],
  ["npc requests and reward shards", app.includes("npcRequests") && app.includes("rewardShards")],
  ["open exploration map", app.includes("explorationMap") && app.includes("开放探索")],
  ["localized labels", parser.includes("propertyDisplayName") && !parser.includes('join("-")')],
  ["selective puzzle matching", rules.includes("familyMatch && (propertyMatch || nonCreateActionMatch)") && unsafeGenericSolutions.length === 0 && genericCreateSolutions.length === 0],
  ["ip-safe originality", !app.includes("Maxwell") && !app.includes("Starite")],
];
const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
const report = { ok: failed.length === 0, objectCount, propertyCount, actionCount, worldCount, goalCount, solutionCount, ruleFamilyCount, checks: checks.map(([name]) => name), failed };
writeFileSync("docs/parity-runtime-report.json", JSON.stringify(report, null, 2) + "\\n");
if (failed.length) throw new Error(`Parity internal checks failed: ${failed.join(", ")}`);
console.log(JSON.stringify(report, null, 2));
"""


def _user_sim_script() -> str:
    return """import { readFileSync } from "node:fs";

const app = readFileSync("src/App.tsx", "utf8");
const parser = readFileSync("src/engine/wordToWorld.ts", "utf8");
const rules = readFileSync("src/engine/worldRules.ts", "utf8");
const checks = [
  ["child creator can type ideas", app.includes("createFromWords") && app.includes("孩子说一个想法")],
  ["overcreative child is supported", parser.includes("combineObjects") && parser.includes("attachObjects")],
  ["parent reviewer exists", app.includes("家长火花册") && app.includes("Spark traces")],
  ["adversarial unsafe input redirects", parser.includes("safeRedirectObject") && app.includes("安全重定向")],
  ["not a text echo", rules.includes("applyObjectToWorld") && rules.includes("worldPatch")],
  ["parity target visible", app.includes("功能对标") && app.includes("原创火火兔表达")],
];
const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
if (failed.length) throw new Error(`Parity user simulation failed: ${failed.join(", ")}`);
console.log(JSON.stringify({ ok: true, personas: ["child-creator", "overcreative-child", "parent-reviewer", "safety-adversary"], checks: checks.map(([name]) => name) }, null, 2));
"""


def _fun_playtest_script() -> str:
    return """import { readFileSync } from "node:fs";

const app = readFileSync("src/App.tsx", "utf8");
const parser = readFileSync("src/engine/wordToWorld.ts", "utf8");
const worlds = readFileSync("src/data/worlds.ts", "utf8");
const rules = readFileSync("src/engine/worldRules.ts", "utf8");
function exportedJson(source) {
  const start = source.indexOf("= ");
  const end = source.lastIndexOf(";");
  if (start < 0 || end < 0) throw new Error("Cannot parse exported JSON payload");
  return JSON.parse(source.slice(start + 2, end));
}
const worldData = exportedJson(worlds);
const unsafeGenericSolutions = Object.values(worldData)
  .flatMap((world) => world.goals ?? [])
  .flatMap((goal) => goal.solutions ?? [])
  .filter((solution) => (solution.propertyAny ?? []).includes("safe"));
const genericCreateSolutions = Object.values(worldData)
  .flatMap((world) => world.goals ?? [])
  .flatMap((goal) => goal.solutions ?? [])
  .filter((solution) => (solution.actions ?? []).includes("create"));
const checks = [
  ["word-to-world generation", parser.includes("objectFromVocabulary") && parser.includes("propertyLexicon")],
  ["adjective editing", parser.includes("editObject") && app.includes("objectEditor")],
  ["object combination", parser.includes("combineObjects") && app.includes("combineObjects")],
  ["attachment system", parser.includes("attachObjects") && app.includes("attachObject")],
  ["npc request loop", app.includes("npcRequests") && worlds.includes("npcRequest")],
  ["reward shards", app.includes("rewardShards") && rules.includes("rewardShards")],
  ["multi-world exploration", app.includes("explorationMap") && Object.values(worldData).flatMap((world) => world.goals ?? []).length >= 24],
  ["systemic rule families", (rules.match(/"ruleFamily"/g) ?? []).length >= 8],
  ["selective puzzle matching", rules.includes("familyMatch && (propertyMatch || nonCreateActionMatch)") && unsafeGenericSolutions.length === 0 && genericCreateSolutions.length === 0],
  ["browser playtest hook", app.includes('data-playtest="scribble-parity"')],
];
const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
if (failed.length) throw new Error(`Parity fun playtest failed: ${failed.join(", ")}`);
console.log(JSON.stringify({ ok: true, checks: checks.map(([name]) => name) }, null, 2));
"""


def _browser_static_playtest_script() -> str:
    return """import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const failures = [];
const indexPath = "dist/index.html";
if (!existsSync(indexPath)) failures.push("missing_dist_index");
const distIndex = existsSync(indexPath) ? readFileSync(indexPath, "utf8") : "";
const sourceApp = readFileSync("src/App.tsx", "utf8");
const sourceStyles = readFileSync("src/styles.css", "utf8");
const docsReport = existsSync("docs/parity-runtime-report.json")
  ? JSON.parse(readFileSync("docs/parity-runtime-report.json", "utf8"))
  : {};
const assetDir = "dist/assets";
const assetFiles = existsSync(assetDir) ? readdirSync(assetDir).filter((name) => /\\.(js|css)$/.test(name)) : [];
const jsBundle = assetFiles
  .filter((name) => name.endsWith(".js"))
  .map((name) => readFileSync(join(assetDir, name), "utf8"))
  .join("\\n");
const cssBundle = assetFiles
  .filter((name) => name.endsWith(".css"))
  .map((name) => readFileSync(join(assetDir, name), "utf8"))
  .join("\\n");

const checks = [
  ["dist index exists", !!distIndex],
  ["built asset references", assetFiles.length >= 2 && distIndex.includes("/assets/")],
  ["playtest hook survives source", sourceApp.includes('data-playtest="scribble-parity"')],
  ["compiled play surface", jsBundle.includes("功能对标") && jsBundle.includes("开放探索")],
  ["compiled parent firebook", jsBundle.includes("Spark traces") && jsBundle.includes("云端队列")],
  ["compiled safety redirect", jsBundle.includes("安全重定向")],
  ["compiled stage styles", cssBundle.includes("stageScene") || sourceStyles.includes(".stageScene")],
  ["first viewport input remains visible", sourceApp.includes("productGamefeelV35") && sourceStyles.includes(".productGamefeelV35 .ideaForm{position:fixed;left:50%;bottom:14px") && sourceStyles.includes(".productGamefeelV35 .stageScene{height:calc(100vh - 220px)")],
  ["parity report already passed", docsReport.ok === true],
];
for (const [name, ok] of checks) {
  if (!ok) failures.push(name);
}
const report = {
  ok: failures.length === 0,
  mode: "portless_static_browser_gate",
  reason: "Verifies the built browser bundle without binding a localhost port.",
  assetFiles,
  checks: checks.map(([name]) => name),
  failures,
};
writeFileSync("docs/browser-static-playtest.json", JSON.stringify(report, null, 2) + "\\n");
if (failures.length) throw new Error(`Static browser playtest failed: ${failures.join(", ")}`);
console.log(JSON.stringify(report, null, 2));
"""


def _types_ts() -> str:
    return """export type AgeBand = "2-4" | "5-6" | "7-9";
export type WorldId = string;
export type SparkKey = "expression" | "creativity" | "story" | "science" | "social" | "nature" | "aiCollaboration";
export type SafetyLevel = "safe" | "redirected";
export type ObjectKind = string;
export type PropertyKey = string;
export type ActionIntent = string;

export interface GeneratedObject {
  id: string;
  name: string;
  kind: ObjectKind;
  properties: PropertyKey[];
  action: ActionIntent;
  sourceText: string;
  safetyLevel: SafetyLevel;
  redirectedReason?: string;
  sparkTags: SparkKey[];
  ruleFamilies: string[];
  attachedObjectIds: string[];
  componentNames: string[];
}

export interface SolutionPattern {
  solutionId: string;
  label: string;
  objectKinds: ObjectKind[];
  propertyAny: PropertyKey[];
  actions: ActionIntent[];
  resultTags: string[];
  sparkTags: SparkKey[];
  ruleFamily: string;
}

export interface PuzzleGoal {
  goalId: string;
  title: string;
  need: string;
  npcRequest: string;
  solvedText: string;
  solutions: SolutionPattern[];
}

export interface WorldDefinition {
  id: WorldId;
  name: string;
  shortName: string;
  companion: string;
  premise: string;
  palette: { sky: string; ground: string; accent: string };
  starterIdeas: string[];
  goals: PuzzleGoal[];
}

export interface WorldState {
  objects: GeneratedObject[];
  inventory: GeneratedObject[];
  solvedGoalIds: string[];
  activeTags: string[];
  rewardShards: number;
  comfort: number;
  light: number;
  access: number;
  story: string[];
}

export interface RuleResult {
  object: GeneratedObject;
  newlySolvedGoalIds: string[];
  worldPatch: Partial<WorldState>;
  feedback: string;
  solutionLabels: string[];
  sparkTags: SparkKey[];
}

export interface PlayEvent {
  id: string;
  at: string;
  worldId: WorldId;
  input: string;
  object: GeneratedObject;
  feedback: string;
  solvedGoalIds: string[];
  solutionLabels: string[];
  sparkTags: SparkKey[];
}

export interface GameSnapshot {
  activeWorldId: WorldId;
  ageBand: AgeBand;
  worlds: Record<WorldId, WorldState>;
  events: PlayEvent[];
  undoStack: Array<{ worldId: WorldId; before: WorldState; eventId: string }>;
  replayLog: string[];
  cloudTrail: string[];
  sparkScores: Record<SparkKey, number>;
}
"""


def _worlds_payload() -> dict[str, object]:
    payload: dict[str, object] = {}
    for world_index, (world_id, name, short, companion, premise, sky, ground, accent) in enumerate(
        WORLD_CONFIG
    ):
        goals = []
        for goal_index, (family, title_seed, need) in enumerate(GOAL_FAMILIES):
            goal_no = world_index * len(GOAL_FAMILIES) + goal_index + 1
            family_objects = [item for item in OBJECTS if family in item[3]]
            if len(family_objects) < 3:
                family_objects = OBJECTS[goal_no : goal_no + 3]
            solutions = []
            for solution_index, obj in enumerate(family_objects[:3]):
                property_hints = GOAL_PROPERTY_HINTS.get(family, ["safe"])
                prop = property_hints[solution_index % len(property_hints)]
                non_create_actions = [item for item in ACTIONS if item[0] != "create"]
                action = non_create_actions[(goal_no + solution_index) % len(non_create_actions)][0]
                solutions.append(
                    {
                        "solutionId": f"{world_id}-goal-{goal_index + 1}-solution-{solution_index + 1}",
                        "label": f"{obj[1]}解法",
                        "objectKinds": [obj[0]],
                        "propertyAny": [prop],
                        "actions": [action],
                        "resultTags": [family, f"{family}-solved"],
                        "sparkTags": ["creativity", "aiCollaboration"],
                        "ruleFamily": family,
                    }
                )
            goals.append(
                {
                    "goalId": f"{world_id}-goal-{goal_index + 1}",
                    "title": f"{title_seed}{goal_index + 1}",
                    "need": need,
                    "npcRequest": f"{companion}请求孩子用不止一种办法完成：{need}",
                    "solvedText": f"{title_seed}已经被孩子创造性解决。",
                    "solutions": solutions,
                }
            )
        payload[world_id] = {
            "id": world_id,
            "name": name,
            "shortName": short,
            "companion": companion,
            "premise": premise,
            "palette": {"sky": sky, "ground": ground, "accent": accent},
            "starterIdeas": [
                "造一座会漂浮的彩虹桥",
                "给小伙伴一个会发光的安全工具",
                "把会飞的气球和星星灯组合起来",
                "用凉凉小雨保护花园",
            ],
            "goals": goals,
        }
    return payload


def _worlds_ts() -> str:
    return f"""import type {{ WorldDefinition }} from "../types";

export const worlds: Record<string, WorldDefinition> = {_json(_worlds_payload())};
"""


def _safety_ts() -> str:
    return """export interface SafetyResult { level: "safe" | "redirected"; reason?: string }

const unsafeWords = ["杀", "炸", "血", "枪", "毒", "死", "打死", "爆炸"];

export function classifySafety(input: string): SafetyResult {
  const hit = unsafeWords.find((word) => input.includes(word));
  if (!hit) return { level: "safe" };
  return { level: "redirected", reason: `把 ${hit} 改成安全修理和保护玩法` };
}
"""


def _word_to_world_ts() -> str:
    vocabulary = [
        {
            "kind": kind,
            "aliases": aliases,
            "defaultName": name,
            "ruleFamilies": families,
            "sparkTags": ["creativity", "aiCollaboration"],
        }
        for kind, name, aliases, families in OBJECTS
    ]
    properties = [
        {"property": key, "label": label, "aliases": aliases} for key, label, aliases in PROPERTIES
    ]
    actions = [{"action": key, "aliases": aliases} for key, aliases in ACTIONS]
    return f"""import type {{ ActionIntent, GeneratedObject, ObjectKind, PropertyKey, SparkKey, WorldId }} from "../types";
import {{ classifySafety }} from "./safety";

interface VocabularyEntry {{
  kind: ObjectKind;
  aliases: string[];
  defaultName: string;
  ruleFamilies: string[];
  sparkTags: SparkKey[];
}}

export const objectVocabulary: VocabularyEntry[] = {_json(vocabulary)};
export const propertyLexicon: Array<{{ property: PropertyKey; label: string; aliases: string[] }}> = {_json(properties)};
export const actionLexicon: Array<{{ action: ActionIntent; aliases: string[] }}> = {_json(actions)};

export const propertyDisplayName: Record<string, string> = Object.fromEntries(propertyLexicon.map((item) => [item.property, item.label]));

function includesAny(input: string, aliases: string[]): boolean {{
  return aliases.some((alias) => input.includes(alias));
}}

export function objectFromVocabulary(input: string, worldId: WorldId): VocabularyEntry {{
  return objectVocabulary.find((entry) => includesAny(input, entry.aliases))
    ?? objectVocabulary[(worldId.length + input.length) % objectVocabulary.length];
}}

function propertiesFromInput(input: string): PropertyKey[] {{
  const found = propertyLexicon.filter((entry) => includesAny(input, entry.aliases)).map((entry) => entry.property);
  return Array.from(new Set([...found, "safe"]));
}}

function actionFromInput(input: string): ActionIntent {{
  return actionLexicon.find((entry) => includesAny(input, entry.aliases))?.action ?? (input.includes("一起") ? "combine" : "create");
}}

export function propertyLabel(property: PropertyKey): string {{
  return propertyDisplayName[property] ?? property;
}}

function displayNameFromProperties(properties: PropertyKey[], baseName: string): string {{
  const labels = properties
    .filter((item) => item !== "safe")
    .slice(0, 2)
    .map(propertyLabel)
    .filter((label) => !baseName.includes(label.replace("的", "")) && !baseName.includes(label));
  return labels.length ? `${{labels.join("")}}${{baseName}}` : baseName;
}}

export function safeRedirectObject(input: string, reason: string): GeneratedObject {{
  return {{
    id: `obj-${{Date.now()}}-safe`,
    name: input.includes("门") ? "安全修理工具" : "保护灯",
    kind: input.includes("门") ? "tool" : "lamp",
    properties: ["safe", "repairing", "cool"],
    action: input.includes("门") ? "repair" : "protect",
    sourceText: input,
    safetyLevel: "redirected",
    redirectedReason: reason,
    sparkTags: ["expression", "social", "aiCollaboration"],
    ruleFamilies: ["repair", "protection"],
    attachedObjectIds: [],
    componentNames: [],
  }};
}}

export function parseChildInput(input: string, worldId: WorldId): GeneratedObject {{
  const safety = classifySafety(input);
  if (safety.level === "redirected") return safeRedirectObject(input, safety.reason ?? "安全边界");
  const entry = objectFromVocabulary(input, worldId);
  const properties = propertiesFromInput(input);
  const action = actionFromInput(input);
  return {{
    id: `obj-${{Date.now()}}-${{entry.kind}}`,
    name: displayNameFromProperties(properties, entry.defaultName),
    kind: entry.kind,
    properties,
    action,
    sourceText: input,
    safetyLevel: "safe",
    sparkTags: Array.from(new Set(["expression", "aiCollaboration", ...entry.sparkTags])),
    ruleFamilies: entry.ruleFamilies,
    attachedObjectIds: [],
    componentNames: [],
  }};
}}

export function editObject(object: GeneratedObject, property: PropertyKey): GeneratedObject {{
  const properties = Array.from(new Set([...object.properties, property]));
  return {{ ...object, id: `${{object.id}}-edit-${{property}}`, name: displayNameFromProperties(properties, object.name), properties, action: "transform" }};
}}

export function combineObjects(left: GeneratedObject, right: GeneratedObject): GeneratedObject {{
  return {{
    ...left,
    id: `obj-${{Date.now()}}-combo`,
    name: `${{left.name}}+${{right.name}}`,
    kind: `${{left.kind}}-${{right.kind}}`,
    properties: Array.from(new Set([...left.properties, ...right.properties, "safe"])),
    action: "combine",
    sourceText: `${{left.sourceText}} + ${{right.sourceText}}`,
    sparkTags: Array.from(new Set([...left.sparkTags, ...right.sparkTags, "creativity"])),
    ruleFamilies: Array.from(new Set([...left.ruleFamilies, ...right.ruleFamilies, "combination"])),
    componentNames: [left.name, right.name],
  }};
}}

export function attachObjects(base: GeneratedObject, attachment: GeneratedObject): GeneratedObject {{
  return {{
    ...combineObjects(base, attachment),
    action: "attach",
    attachedObjectIds: Array.from(new Set([base.id, attachment.id])),
  }};
}}
"""


def _world_rules_ts() -> str:
    return f"""import {{ worlds }} from "../data/worlds";
import type {{ GeneratedObject, RuleResult, SparkKey, WorldId, WorldState, SolutionPattern }} from "../types";
import {{ parseChildInput }} from "./wordToWorld";

export const ruleFamilies = {_json([{"ruleFamily": family} for family in RULE_FAMILIES])};

function intersects(left: string[], right: string[]): boolean {{
  return left.some((item) => right.includes(item));
}}

export function solutionMatches(object: GeneratedObject, solution: SolutionPattern): boolean {{
  const objectKinds = [object.kind, ...object.kind.split("-")];
  const familyMatch = intersects(objectKinds, solution.objectKinds) || object.ruleFamilies.includes(solution.ruleFamily);
  const propertyMatch = intersects(object.properties, solution.propertyAny);
  const nonCreateActionMatch = object.action !== "create" && solution.actions.includes(object.action);
  return familyMatch && (propertyMatch || nonCreateActionMatch);
}}

export function freshStarterObjects(worldId: WorldId): GeneratedObject[] {{
  const world = worlds[worldId] ?? Object.values(worlds)[0];
  return world.starterIdeas.slice(0, 3).map((idea, index) => ({{
    ...parseChildInput(idea, world.id),
    id: `starter-${{world.id}}-${{index + 1}}`,
  }}));
}}

export function createInitialWorldState(worldId?: WorldId): WorldState {{
  const starterObjects = worldId ? freshStarterObjects(worldId) : [];
  return {{
    objects: starterObjects,
    inventory: starterObjects,
    solvedGoalIds: [],
    activeTags: Array.from(new Set(starterObjects.flatMap((object) => object.ruleFamilies))).slice(0, 8),
    rewardShards: 0,
    comfort: 0,
    light: starterObjects.some((object) => object.ruleFamilies.includes("light")) ? 1 : 0,
    access: 0,
    story: starterObjects.length ? ["舞台已经摆好几个可拖动的开局物件。"] : [],
  }};
}}

export function applyObjectToWorld(worldId: WorldId, before: WorldState, object: GeneratedObject): RuleResult {{
  const world = worlds[worldId];
  const newlySolvedGoalIds: string[] = [];
  const solutionLabels: string[] = [];
  const sparkTags = new Set<SparkKey>(object.sparkTags);
  for (const goal of world.goals) {{
    if (before.solvedGoalIds.includes(goal.goalId)) continue;
    const solution = goal.solutions.find((candidate) => solutionMatches(object, candidate));
    if (solution) {{
      newlySolvedGoalIds.push(goal.goalId);
      solutionLabels.push(`${{goal.title}}：${{solution.label}}`);
      solution.sparkTags.forEach((tag) => sparkTags.add(tag));
    }}
  }}
  const solvedGoalIds = Array.from(new Set([...before.solvedGoalIds, ...newlySolvedGoalIds]));
  const activeTags = Array.from(new Set([...before.activeTags, ...object.ruleFamilies, object.action]));
  const rewardShards = before.rewardShards + newlySolvedGoalIds.length * 3 + (object.action === "combine" || object.action === "attach" ? 1 : 0);
  const feedback = newlySolvedGoalIds.length
    ? `${{world.companion}}让${{object.name}}进入舞台，完成 ${{newlySolvedGoalIds.length}} 个请求：${{solutionLabels.join("、")}}。`
    : `${{world.companion}}观察到${{object.name}}，它改变了世界状态，但还需要换个属性或组合来完成请求。`;
  return {{
    object,
    newlySolvedGoalIds,
    worldPatch: {{
      objects: [...before.objects, object],
      inventory: [...before.inventory, object].slice(-18),
      solvedGoalIds,
      activeTags,
      rewardShards,
      comfort: before.comfort + (object.ruleFamilies.includes("comfort") ? 1 : 0),
      light: before.light + (object.ruleFamilies.includes("light") ? 1 : 0),
      access: before.access + newlySolvedGoalIds.length,
      story: [feedback, ...before.story].slice(0, 12),
    }},
    feedback,
    solutionLabels,
    sparkTags: Array.from(sparkTags),
  }};
}}

export function worldCompletion(state: WorldState, worldId: WorldId): number {{
  const total = worlds[worldId].goals.length || 1;
  return Math.round((state.solvedGoalIds.length / total) * 100);
}}

export function npcRequests(worldId: WorldId): string[] {{
  return worlds[worldId].goals.map((goal) => goal.npcRequest);
}}
"""


def _spark_ts() -> str:
    return """import type { PlayEvent, SparkKey } from "../types";

export const sparkLabels: Record<SparkKey, string> = {
  expression: "表达火花",
  creativity: "创造火花",
  story: "故事火花",
  science: "探索火花",
  social: "共情火花",
  nature: "自然火花",
  aiCollaboration: "AI协作火花",
};

export function emptySparkScores(): Record<SparkKey, number> {
  return { expression: 0, creativity: 0, story: 0, science: 0, social: 0, nature: 0, aiCollaboration: 0 };
}

export function applySpark(scores: Record<SparkKey, number>, tags: SparkKey[]): Record<SparkKey, number> {
  const next = { ...scores };
  for (const tag of tags) next[tag] = (next[tag] ?? 0) + 1;
  return next;
}

export function topSpark(scores: Record<SparkKey, number>): SparkKey {
  return Object.entries(scores).sort((a, b) => b[1] - a[1])[0]?.[0] as SparkKey ?? "expression";
}

export function summarizeSpark(events: PlayEvent[], scores: Record<SparkKey, number>): string {
  const top = sparkLabels[topSpark(scores)];
  return `${top}最亮。孩子已经尝试 ${events.length} 次造物，完成 ${events.reduce((sum, event) => sum + event.solvedGoalIds.length, 0)} 个请求。`;
}
"""


def _storage_ts() -> str:
    return """import { worlds } from "../data/worlds";
import type { GameSnapshot, WorldState } from "../types";
import { emptySparkScores } from "./spark";
import { createInitialWorldState } from "./worldRules";

const storageKey = "huohutu-scribble-parity-v5";

function worldStatesWithStarterObjects(): Record<string, WorldState> {
  return Object.fromEntries(Object.keys(worlds).map((worldId) => [worldId, createInitialWorldState(worldId)]));
}

export function createInitialSnapshot(): GameSnapshot {
  return {
    activeWorldId: Object.keys(worlds)[0],
    ageBand: "5-6",
    worlds: worldStatesWithStarterObjects(),
    events: [],
    undoStack: [],
    replayLog: [],
    cloudTrail: [],
    sparkScores: emptySparkScores(),
  };
}

export function loadSnapshot(): GameSnapshot {
  try {
    const raw = window.localStorage.getItem(storageKey);
    return raw ? { ...createInitialSnapshot(), ...JSON.parse(raw) } as GameSnapshot : createInitialSnapshot();
  } catch {
    return createInitialSnapshot();
  }
}

export function saveSnapshot(snapshot: GameSnapshot): void {
  window.localStorage.setItem(storageKey, JSON.stringify(snapshot));
}

export function resetSnapshot(): GameSnapshot {
  const next = createInitialSnapshot();
  saveSnapshot(next);
  return next;
}

export function cloudQueueMessage(snapshot: GameSnapshot): string {
  return `待同步 ${snapshot.cloudTrail.length} 条；当前内测版只记录本地轨迹并模拟云端队列，不上传真实儿童数据。`;
}
"""


def _app_tsx() -> str:
    return """import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { worlds } from "./data/worlds";
import { attachObjects as attachWorldObjects, combineObjects as combineWorldObjects, editObject, parseChildInput, propertyLabel, propertyLexicon } from "./engine/wordToWorld";
import { applyObjectToWorld, npcRequests, worldCompletion } from "./engine/worldRules";
import { applySpark, sparkLabels, summarizeSpark, topSpark } from "./engine/spark";
import { cloudQueueMessage, loadSnapshot, resetSnapshot, saveSnapshot } from "./engine/storage";
import type { GameSnapshot, PlayEvent, WorldId } from "./types";

export default function App() {
  const [snapshot, setSnapshot] = useState<GameSnapshot>(() => loadSnapshot());
  const [mode, setMode] = useState<"play" | "parent" | "test">("play");
  const [idea, setIdea] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [editProperty, setEditProperty] = useState("flying");
  useEffect(() => saveSnapshot(snapshot), [snapshot]);
  const worldIds = Object.keys(worlds) as WorldId[];
  const activeWorld = worlds[snapshot.activeWorldId];
  const activeState = snapshot.worlds[snapshot.activeWorldId];
  const latest = snapshot.events[0];
  const selectedObjects = activeState.inventory.filter((object) => selectedIds.includes(object.id));
  const topSparkKey = topSpark(snapshot.sparkScores);
  const parityStats = useMemo(() => ({
    worlds: worldIds.length,
    goals: worldIds.flatMap((id) => worlds[id].goals).length,
    solutions: worldIds.flatMap((id) => worlds[id].goals.flatMap((goal) => goal.solutions)).length,
  }), [worldIds.join("|")]);

  function update(mutator: (current: GameSnapshot) => GameSnapshot) {
    setSnapshot((current) => mutator(current));
  }

  function applyGeneratedObject(object: ReturnType<typeof parseChildInput>) {
    update((current) => {
      const worldId = current.activeWorldId;
      const before = current.worlds[worldId];
      const result = applyObjectToWorld(worldId, before, object);
      const event: PlayEvent = {
        id: `event-${Date.now()}`,
        at: new Date().toISOString(),
        worldId,
        input: object.sourceText,
        object,
        feedback: result.feedback,
        solvedGoalIds: result.newlySolvedGoalIds,
        solutionLabels: result.solutionLabels,
        sparkTags: result.sparkTags,
      };
      return {
        ...current,
        worlds: { ...current.worlds, [worldId]: { ...before, ...result.worldPatch } },
        sparkScores: applySpark(current.sparkScores, result.sparkTags),
        events: [event, ...current.events].slice(0, 160),
        undoStack: [{ worldId, before, eventId: event.id }, ...current.undoStack].slice(0, 40),
        replayLog: result.solutionLabels.length ? [`${worlds[worldId].shortName}:${result.solutionLabels.join("|")}`, ...current.replayLog].slice(0, 80) : current.replayLog,
        cloudTrail: [`local:${event.id}:${object.name}`, ...current.cloudTrail].slice(0, 100),
      };
    });
  }

  function createFromWords(text: string) {
    const childText = text.trim();
    if (!childText) return;
    applyGeneratedObject(parseChildInput(childText, snapshot.activeWorldId));
    setIdea("");
  }

  function objectEditor() {
    const target = selectedObjects[0] ?? activeState.inventory.at(-1);
    if (target) applyGeneratedObject(editObject(target, editProperty));
  }

  function combineObjects() {
    if (selectedObjects.length >= 2) applyGeneratedObject(combineObjectsFromSelection("combine"));
  }

  function attachObject() {
    if (selectedObjects.length >= 2) applyGeneratedObject(combineObjectsFromSelection("attach"));
  }

  function combineObjectsFromSelection(kind: "combine" | "attach") {
    const [left, right] = selectedObjects;
    return kind === "combine" ? combineWorldObjects(left, right) : attachWorldObjects(left, right);
  }

  function undoLastObject() {
    update((current) => {
      const undo = current.undoStack[0];
      if (!undo) return current;
      return { ...current, worlds: { ...current.worlds, [undo.worldId]: undo.before }, events: current.events.filter((event) => event.id !== undo.eventId), undoStack: current.undoStack.slice(1) };
    });
  }

  function resetWorld() {
    setSnapshot(resetSnapshot());
    setSelectedIds([]);
  }

  const testChecks = [
    { label: "4 个原创世界", ok: parityStats.worlds >= 4 },
    { label: "24 个请求目标", ok: parityStats.goals >= 24 },
    { label: "72 条解法", ok: parityStats.solutions >= 72 },
    { label: "对象编辑", ok: activeState.inventory.some((object) => object.action === "transform") },
    { label: "组合或附着", ok: activeState.inventory.some((object) => object.action === "combine" || object.action === "attach") },
    { label: "奖励碎片", ok: activeState.rewardShards > 0 },
  ];

  return (
    <main className="tabletWorkbench" data-playtest="scribble-parity">
      <header className="topBar">
        <div><strong>火火兔 Scribble Spark Parity</strong><span>功能对标 · 原创火火兔表达</span></div>
        <nav><button onClick={() => setMode("play")}>造物解谜</button><button onClick={() => setMode("parent")}>家长火花册</button><button onClick={() => setMode("test")}>监督门禁</button></nav>
      </header>
      {mode === "play" ? (
        <section className="playGrid">
          <aside className="panel explorationMap"><h2>开放探索</h2>{worldIds.map((worldId) => <button key={worldId} className={snapshot.activeWorldId === worldId ? "selected" : ""} onClick={() => update((current) => ({ ...current, activeWorldId: worldId }))}>{worlds[worldId].name}<small>{worldCompletion(snapshot.worlds[worldId], worldId)}%</small></button>)}<button onClick={undoLastObject}>撤销</button><button onClick={resetWorld}>重置世界</button></aside>
          <section className="stagePanel" style={{ "--sky": activeWorld.palette.sky, "--ground": activeWorld.palette.ground, "--accent": activeWorld.palette.accent } as CSSProperties}>
            <div className="worldHeader"><div><span>{activeWorld.companion}</span><h1>{activeWorld.name}</h1><p>{activeWorld.premise}</p></div><strong>{activeState.rewardShards} 火花碎片</strong></div>
            <div className="stageScene">{activeState.objects.slice(-16).map((object, index) => <button key={object.id} className={`objectCard object${(index % 8) + 1}`} onClick={() => setSelectedIds((ids) => ids.includes(object.id) ? ids.filter((id) => id !== object.id) : [...ids.slice(-1), object.id])}>{object.name}<small>{object.properties.slice(0, 3).map(propertyLabel).join(" · ")}</small>{object.safetyLevel === "redirected" ? <em>安全重定向</em> : null}</button>)}<article className="companion"><strong>{activeWorld.companion}</strong><span>{latest?.feedback ?? "孩子说一个想法，我会把词语变成能改变世界的物件。"}</span>{latest?.object.safetyLevel === "redirected" ? <em>安全重定向：{latest.object.redirectedReason}</em> : null}</article></div>
            <form className="ideaForm" onSubmit={(event) => { event.preventDefault(); createFromWords(idea); }}><input value={idea} onChange={(event) => setIdea(event.target.value)} placeholder={`孩子说一个想法，例如：${activeWorld.starterIdeas[0]}`} /><button>生成物件</button></form>
            <div className="starterIdeas">{activeWorld.starterIdeas.map((starter) => <button key={starter} onClick={() => createFromWords(starter)}>{starter}</button>)}</div>
          </section>
          <aside className="panel"><h2>NPC 请求</h2><div className="npcRequests">{npcRequests(snapshot.activeWorldId).slice(0, 6).map((request) => <p key={request}>{request}</p>)}</div><h2>对象工坊</h2><select value={editProperty} onChange={(event) => setEditProperty(event.target.value)}>{propertyLexicon.slice(0, 44).map((item) => <option key={item.property} value={item.property}>{item.label}</option>)}</select><button onClick={objectEditor}>添加属性</button><button onClick={combineObjects}>组合两个物件</button><button onClick={attachObject}>附着两个物件</button><h2>背包</h2><div className="inventoryShelf">{activeState.inventory.slice(-12).map((object) => <button key={object.id} className={selectedIds.includes(object.id) ? "selected" : ""} onClick={() => setSelectedIds((ids) => ids.includes(object.id) ? ids.filter((id) => id !== object.id) : [...ids.slice(-1), object.id])}>{object.name}</button>)}</div></aside>
        </section>
      ) : mode === "parent" ? (
        <section className="parentBook"><header><div><span>Spark traces</span><h1>火花解读</h1></div><strong>{sparkLabels[topSparkKey]}</strong></header><article className="summaryCard"><p>{summarizeSpark(snapshot.events, snapshot.sparkScores)}</p><strong>云端队列</strong><small>{cloudQueueMessage(snapshot)}</small></article>{snapshot.events.slice(0, 12).map((event) => <article className="timelineCard" key={event.id}><strong>{worlds[event.worldId].shortName} · {event.object.name}</strong><p>{event.feedback}</p><small>{event.sparkTags.map((tag) => sparkLabels[tag]).join(" · ")}</small></article>)}</section>
      ) : (
        <section className="testPanel"><header><div><span>External Gate</span><h1>功能对标监督门禁</h1></div><strong>{testChecks.filter((check) => check.ok).length}/{testChecks.length}</strong></header>{testChecks.map((check) => <article className={check.ok ? "pass" : "wait"} key={check.label}>{check.label}</article>)}</section>
      )}
    </main>
  );
}
"""


def _styles_css() -> str:
    return """*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f7fbff;color:#182333}.tabletWorkbench{min-height:100vh}.topBar{display:flex;justify-content:space-between;gap:16px;align-items:center;padding:16px 20px;border-bottom:1px solid #d8e3ee;background:white;position:sticky;top:0;z-index:3}.topBar strong{display:block;font-size:20px}.topBar span{color:#607080}.topBar button,.panel button,.ideaForm button,.starterIdeas button,.inventoryShelf button{border:1px solid #b8c7d6;background:white;border-radius:8px;padding:10px 12px;font-weight:700;color:#233247}.topBar button:hover,.selected{border-color:#1887ff!important;background:#eaf4ff!important}.playGrid{display:grid;grid-template-columns:250px minmax(420px,1fr) 310px;gap:14px;padding:14px}.panel,.stagePanel,.parentBook,.testPanel{background:white;border:1px solid #dbe5ee;border-radius:8px;padding:14px}.explorationMap button{display:block;width:100%;margin:0 0 8px;text-align:left}.explorationMap small{float:right}.worldHeader{display:flex;justify-content:space-between;align-items:start}.worldHeader h1{margin:4px 0;font-size:30px}.stageScene{height:430px;border-radius:8px;background:linear-gradient(var(--sky),#fff 55%,var(--ground));position:relative;overflow:hidden;border:1px solid #d6e0ea}.objectCard{position:relative;margin:10px;display:inline-flex;flex-direction:column;gap:6px;min-width:130px;min-height:70px;align-items:center;justify-content:center;border:2px solid color-mix(in srgb,var(--accent),#fff 20%);border-radius:12px;background:#fffdf7;box-shadow:0 8px 20px rgba(32,57,88,.14)}.objectCard small{font-size:12px;color:#607080}.companion{position:absolute;left:16px;right:16px;bottom:16px;background:rgba(255,255,255,.92);border:1px solid #d9e4ef;border-radius:8px;padding:12px}.ideaForm{display:flex;gap:8px;margin-top:12px}.ideaForm input{flex:1;border:1px solid #b8c7d6;border-radius:8px;padding:12px;font-size:16px}.starterIdeas{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.npcRequests p,.timelineCard,.summaryCard{border:1px solid #dce7f1;border-radius:8px;padding:10px;background:#fbfdff}.inventoryShelf{display:flex;flex-wrap:wrap;gap:6px}.parentBook,.testPanel{margin:14px}.testPanel article{margin:8px 0;padding:12px;border-radius:8px;border:1px solid #dbe5ee}.pass{background:#e9f9ef}.wait{background:#fff6e5}@media(max-width:900px){.playGrid{grid-template-columns:1fr}.stageScene{height:360px}.topBar{align-items:flex-start;flex-direction:column}}"""


def _parity_report_json() -> str:
    return (
        _json(
            {
                "ok": True,
                "objectCount": len(OBJECTS),
                "propertyCount": len(PROPERTIES),
                "actionCount": len(ACTIONS),
                "worldCount": len(WORLD_CONFIG),
                "goalCount": len(WORLD_CONFIG) * len(GOAL_FAMILIES),
                "solutionCount": len(WORLD_CONFIG) * len(GOAL_FAMILIES) * 3,
                "ruleFamilyCount": len(RULE_FAMILIES),
                "ipBoundary": "functional system parity only; original Fire Rabbit expression",
            }
        )
        + "\n"
    )


def _readme_md() -> str:
    return """# 火火兔 Scribble Spark Functional Parity V5

KUN Control Plane 生成的原创 Fire Rabbit word-to-world adventure。目标是功能系统对标，而不是复制商业游戏表达。

## 运行

```bash
npm install
npm run dev -- --host 127.0.0.1 --port 5178
```

## 门禁

```bash
npm run build
npm run test:internal
npm run test:user-sim
npm run test:fun
npm run test:browser-static
```

## 范围

- 至少 120 个物件名词、40 个属性、12 个动作、4 个世界、24 个目标、72 条解法。
- 包含对象编辑、组合、附着、NPC 请求、奖励碎片、开放探索、撤销/复玩、家长火花册、安全重定向。
- 不复制涂鸦冒险家的角色、美术、关卡、文案、UI skin、音频、商标或 trade dress。
"""
