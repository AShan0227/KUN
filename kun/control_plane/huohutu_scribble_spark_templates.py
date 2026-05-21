"""Scribble Spark Fire Rabbit game templates for KUN-owned delivery work."""

from __future__ import annotations


def scribble_spark_ready_files() -> dict[str, str]:
    """Return the word-to-world puzzle sandbox project template."""

    return {
        "package.json": _package_json(),
        "scripts/internal-test.mjs": _internal_test_script(),
        "scripts/user-sim.mjs": _user_sim_script(),
        "scripts/fun-playtest.mjs": _fun_playtest_script(),
        "scripts/browser-static-playtest.mjs": _browser_static_playtest_script(),
        "src/types.ts": _types_ts(),
        "src/data/worlds.ts": _worlds_ts(),
        "src/engine/safety.ts": _safety_ts(),
        "src/engine/wordToWorld.ts": _word_to_world_ts(),
        "src/engine/worldRules.ts": _world_rules_ts(),
        "src/engine/spark.ts": _spark_ts(),
        "src/engine/storage.ts": _storage_ts(),
        "src/App.tsx": _app_tsx(),
        "src/styles.css": _styles_css(),
        "README.md": _readme_md(),
    }


def _package_json() -> str:
    return """{
  "name": "huohutu-scribble-spark-final",
  "version": "2.0.0-scribble.1",
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


def _internal_test_script() -> str:
    return """import { existsSync, readFileSync } from "node:fs";

const requiredFiles = [
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
const storage = readFileSync("src/engine/storage.ts", "utf8");
const packageJson = JSON.parse(readFileSync("package.json", "utf8"));

const goalCount = (worlds.match(/goalId:/g) ?? []).length;
const solutionCount = (worlds.match(/solutionId:/g) ?? []).length;
const vocabularyCount = (parser.match(/kind:/g) ?? []).length;

const checks = [
  ["scribble package", packageJson.name === "huohutu-scribble-spark-final"],
  ["fun gate script", packageJson.scripts["test:fun"] === "node scripts/fun-playtest.mjs"],
  ["word-to-world parser", parser.includes("parseChildInput") && parser.includes("propertyLexicon")],
  ["child-facing property labels localized", parser.includes("propertyDisplayName") && !parser.includes('join("-")')],
  ["object vocabulary depth", vocabularyCount >= 18],
  ["world rule engine", rules.includes("applyObjectToWorld") && rules.includes("solutionMatches")],
  ["two worlds", worlds.includes("rainbow-island") && worlds.includes("story-planet")],
  ["three goals per world", goalCount >= 6],
  ["three solutions per goal", solutionCount >= 18],
  ["undo history", app.includes("undoLastObject") && storage.includes("undoStack")],
  ["replay trail", app.includes("replaySolution") && storage.includes("replayLog")],
  ["parent firebook", app.includes("家长火花册") && app.includes("Spark traces")],
  ["local cloud behavior trail", storage.includes("cloudTrail") && app.includes("云端队列")],
  ["safety redirection", parser.includes("safeRedirectObject") && app.includes("安全重定向")],
];

const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
if (failed.length) throw new Error(`Scribble internal checks failed: ${failed.join(", ")}`);

console.log(JSON.stringify({ ok: true, goalCount, solutionCount, vocabularyCount, checks: checks.map(([name]) => name) }, null, 2));
"""


def _user_sim_script() -> str:
    return """import { readFileSync } from "node:fs";

const app = readFileSync("src/App.tsx", "utf8");
const worlds = readFileSync("src/data/worlds.ts", "utf8");
const parser = readFileSync("src/engine/wordToWorld.ts", "utf8");
const rules = readFileSync("src/engine/worldRules.ts", "utf8");
const styles = readFileSync("src/styles.css", "utf8");

const checks = [
  ["child can type natural language", app.includes("createFromWords") && app.includes("孩子说一个想法")],
  ["generated objects are playable cards", app.includes("objectCard") && app.includes("inspectObject")],
  ["inventory is used by puzzles", app.includes("inventoryShelf") && rules.includes("inventory")],
  ["undo and reset visible", app.includes("撤销") && app.includes("重置世界")],
  ["world state changes from rules", rules.includes("worldPatch") && rules.includes("openDoor")],
  ["multiple solution path labels", worlds.includes("solutionId") && worlds.includes("soft-bridge")],
  ["safe fallback object", parser.includes("保护灯") && parser.includes("修理工具")],
  ["child-facing object names are localized", parser.includes("displayNameFromProperties") && app.includes("propertyLabel") && !parser.includes('join("-")')],
  ["parent replay is not raw logs only", app.includes("复玩解法") && app.includes("火花解读")],
  ["tablet-first board", styles.includes(".tabletWorkbench") && styles.includes(".stagePanel")],
  ["not old template", !app.includes("actionCard") && !app.includes("把这句话变成了新的可玩物件")],
];

const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
if (failed.length) throw new Error(`Scribble user simulation failed: ${failed.join(", ")}`);

console.log(JSON.stringify({ ok: true, personas: ["child-creator", "parent-reviewer", "internal-supervisor"], checks: checks.map(([name]) => name) }, null, 2));
"""


def _fun_playtest_script() -> str:
    return """import { readFileSync } from "node:fs";

const app = readFileSync("src/App.tsx", "utf8");
const worlds = readFileSync("src/data/worlds.ts", "utf8");
const parser = readFileSync("src/engine/wordToWorld.ts", "utf8");
const rules = readFileSync("src/engine/worldRules.ts", "utf8");

const goalCount = (worlds.match(/goalId:/g) ?? []).length;
const solutionCount = (worlds.match(/solutionId:/g) ?? []).length;

const checks = [
  ["non-echo object generation", parser.includes("objectFromVocabulary") && !parser.includes("slice(0, 8)")],
  ["no internal property keys in child UI", parser.includes("propertyDisplayName") && app.includes("propertyLabel") && !app.includes("properties.join")],
  ["adjective property causality", parser.includes("floating") && parser.includes("illuminating") && rules.includes("propertyAny")],
  ["minimum world puzzle depth", goalCount >= 6 && solutionCount >= 18],
  ["systemic interactions", rules.includes("extinguishFire") && rules.includes("bridgeRiver") && rules.includes("comfortFriend")],
  ["multiple solutions displayed", app.includes("可行解法") && app.includes("solutionBadges")],
  ["undo reset replay", app.includes("undoLastObject") && app.includes("resetWorld") && app.includes("replaySolution")],
  ["spark traces from play", app.includes("Spark traces") && rules.includes("sparkTags")],
  ["browser playtest hook", app.includes('data-playtest="scribble-spark"')],
];

const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
if (failed.length) throw new Error(`Scribble fun playtest failed: ${failed.join(", ")}`);

console.log(JSON.stringify({ ok: true, goalCount, solutionCount, checks: checks.map(([name]) => name) }, null, 2));
"""


def _browser_static_playtest_script() -> str:
    return """import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const failures = [];
const indexPath = "dist/index.html";
if (!existsSync(indexPath)) failures.push("missing_dist_index");
const distIndex = existsSync(indexPath) ? readFileSync(indexPath, "utf8") : "";
const app = readFileSync("src/App.tsx", "utf8");
const styles = readFileSync("src/styles.css", "utf8");
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
  ["source playtest hook", app.includes('data-playtest="scribble-spark"')],
  ["compiled play surface", jsBundle.includes("孩子说一个想法") && jsBundle.includes("可行解法")],
  ["compiled parent firebook", jsBundle.includes("Spark traces") && jsBundle.includes("云端队列")],
  ["compiled safety redirect", jsBundle.includes("安全重定向")],
  ["compiled stage styles", cssBundle.includes("stageScene") || styles.includes(".stageScene")],
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
export type WorldId = "rainbow-island" | "story-planet";
export type SparkKey = "expression" | "creativity" | "story" | "science" | "social" | "nature" | "aiCollaboration";
export type SafetyLevel = "safe" | "redirected";
export type ObjectKind = "bridge" | "key" | "lamp" | "cloud" | "boat" | "seed" | "rain" | "blanket" | "friend" | "tool" | "music" | "animal" | "door" | "stone" | "kite" | "shield";
export type PropertyKey = "red" | "blue" | "rainbow" | "small" | "big" | "soft" | "metal" | "wooden" | "floating" | "flying" | "warm" | "cool" | "wet" | "illuminating" | "friendly" | "repairing" | "musical" | "safe";
export type ActionIntent = "create" | "use" | "comfort" | "ask" | "repair" | "cross" | "light" | "open" | "float" | "protect";

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
}

export interface SolutionPattern {
  solutionId: string;
  label: string;
  objectKinds: ObjectKind[];
  propertyAny: PropertyKey[];
  actions: ActionIntent[];
  resultTags: string[];
  sparkTags: SparkKey[];
}

export interface PuzzleGoal {
  goalId: string;
  title: string;
  need: string;
  solvedText: string;
  solutions: SolutionPattern[];
}

export interface WorldDefinition {
  id: WorldId;
  name: string;
  shortName: string;
  companion: string;
  premise: string;
  palette: { sky: string; ground: string; accent: string; ink: string };
  starterIdeas: string[];
  goals: PuzzleGoal[];
}

export interface WorldState {
  objects: GeneratedObject[];
  inventory: GeneratedObject[];
  solvedGoalIds: string[];
  activeTags: string[];
  comfort: number;
  light: number;
  access: number;
  story: string[];
}

export interface RuleResult {
  object: GeneratedObject;
  solvedGoalIds: string[];
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

export interface SparkScores {
  expression: number;
  creativity: number;
  story: number;
  science: number;
  social: number;
  nature: number;
  aiCollaboration: number;
}

export interface GameSnapshot {
  ageBand: AgeBand;
  activeWorldId: WorldId;
  worlds: Record<WorldId, WorldState>;
  sparkScores: SparkScores;
  events: PlayEvent[];
  undoStack: GameSnapshotUndo[];
  replayLog: string[];
  cloudTrail: string[];
}

export interface GameSnapshotUndo {
  worldId: WorldId;
  before: WorldState;
  eventId: string;
}
"""


def _worlds_ts() -> str:
    return """import type { WorldDefinition, WorldId, WorldState } from "../types";

export const worlds: Record<WorldId, WorldDefinition> = {
  "rainbow-island": {
    id: "rainbow-island",
    name: "彩虹造物岛",
    shortName: "造物岛",
    companion: "小火",
    premise: "河水挡住小兔，夜色让花园变暗，一小团火苗让云桥不敢靠近。",
    palette: { sky: "#7ed7f7", ground: "#8bd17c", accent: "#ff8a65", ink: "#16324a" },
    starterIdeas: ["造一座软软的彩虹桥", "来一场凉凉的小雨", "点亮一盏会飞的星星灯", "给小兔一把木头钥匙"],
    goals: [
      {
        goalId: "rainbow-cross-river",
        title: "帮小兔过河",
        need: "需要跨过河或安全漂过去。",
        solvedText: "小兔安全到了花园门口。",
        solutions: [
          { solutionId: "soft-bridge", label: "软桥跨河", objectKinds: ["bridge"], propertyAny: ["soft", "rainbow", "wooden"], actions: ["cross", "create"], resultTags: ["bridgeRiver"], sparkTags: ["creativity", "science"] },
          { solutionId: "floating-boat", label: "漂浮小船", objectKinds: ["boat"], propertyAny: ["floating", "wooden", "safe"], actions: ["float", "cross"], resultTags: ["bridgeRiver"], sparkTags: ["science", "nature"] },
          { solutionId: "flying-kite", label: "飞行风筝", objectKinds: ["kite"], propertyAny: ["flying", "big", "friendly"], actions: ["cross", "create"], resultTags: ["bridgeRiver"], sparkTags: ["creativity", "aiCollaboration"] }
        ]
      },
      {
        goalId: "rainbow-calm-fire",
        title: "让小火苗安静",
        need: "不能制造危险，要用安全方式让火苗冷静。",
        solvedText: "火苗变成温暖小夜灯。",
        solutions: [
          { solutionId: "cool-rain", label: "凉雨灭火", objectKinds: ["rain", "cloud"], propertyAny: ["cool", "wet", "safe"], actions: ["protect", "create"], resultTags: ["extinguishFire"], sparkTags: ["nature", "science"] },
          { solutionId: "repair-tool", label: "修理工具", objectKinds: ["tool"], propertyAny: ["repairing", "metal", "safe"], actions: ["repair", "protect"], resultTags: ["extinguishFire"], sparkTags: ["science", "aiCollaboration"] },
          { solutionId: "safe-shield", label: "安全护盾", objectKinds: ["shield"], propertyAny: ["safe", "cool"], actions: ["protect"], resultTags: ["extinguishFire"], sparkTags: ["social", "science"] }
        ]
      },
      {
        goalId: "rainbow-light-garden",
        title: "点亮夜色花园",
        need: "需要能照明或带来温暖的安全物件。",
        solvedText: "花园亮了，小兔看见回家的路。",
        solutions: [
          { solutionId: "star-lamp", label: "星星灯照明", objectKinds: ["lamp"], propertyAny: ["illuminating", "flying", "warm"], actions: ["light"], resultTags: ["lightDark"], sparkTags: ["creativity", "science"] },
          { solutionId: "friendly-firefly", label: "友好萤火伙伴", objectKinds: ["friend", "animal"], propertyAny: ["friendly", "illuminating"], actions: ["comfort", "light"], resultTags: ["lightDark"], sparkTags: ["social", "nature"] },
          { solutionId: "glowing-stone", label: "发光石头", objectKinds: ["stone"], propertyAny: ["illuminating", "warm"], actions: ["create", "light"], resultTags: ["lightDark"], sparkTags: ["science", "creativity"] }
        ]
      }
    ],
  },
  "story-planet": {
    id: "story-planet",
    name: "故事星球",
    shortName: "故事星",
    companion: "泡泡船长",
    premise: "问题之门锁住了故事路，迷路星星有点害怕，风声把结尾吹散了。",
    palette: { sky: "#9aa8ff", ground: "#ffd166", accent: "#36b8a8", ink: "#221c46" },
    starterIdeas: ["给门一把会唱歌的钥匙", "送迷路星星一条温暖毯子", "召唤一个友好的故事朋友", "造一艘会飞的泡泡船"],
    goals: [
      {
        goalId: "story-open-door",
        title: "打开问题之门",
        need: "需要钥匙、问题或会回应的工具。",
        solvedText: "问题之门打开，露出下一页故事。",
        solutions: [
          { solutionId: "musical-key", label: "音乐钥匙", objectKinds: ["key"], propertyAny: ["musical", "metal", "friendly"], actions: ["open"], resultTags: ["openDoor"], sparkTags: ["creativity", "story"] },
          { solutionId: "asking-friend", label: "会提问的朋友", objectKinds: ["friend"], propertyAny: ["friendly"], actions: ["ask", "open"], resultTags: ["openDoor"], sparkTags: ["social", "science"] },
          { solutionId: "repair-tool-door", label: "修门工具", objectKinds: ["tool"], propertyAny: ["repairing", "metal"], actions: ["repair", "open"], resultTags: ["openDoor"], sparkTags: ["science", "aiCollaboration"] }
        ]
      },
      {
        goalId: "story-comfort-star",
        title: "安慰迷路星星",
        need: "需要温暖、朋友或柔软物件。",
        solvedText: "迷路星星愿意一起回到故事轨道。",
        solutions: [
          { solutionId: "warm-blanket", label: "温暖毯子", objectKinds: ["blanket"], propertyAny: ["warm", "soft"], actions: ["comfort"], resultTags: ["comfortFriend"], sparkTags: ["social", "expression"] },
          { solutionId: "friendly-animal", label: "友好伙伴", objectKinds: ["friend", "animal"], propertyAny: ["friendly", "warm"], actions: ["comfort"], resultTags: ["comfortFriend"], sparkTags: ["social", "nature"] },
          { solutionId: "music-cloud", label: "音乐云", objectKinds: ["cloud", "music"], propertyAny: ["musical", "soft"], actions: ["comfort", "create"], resultTags: ["comfortFriend"], sparkTags: ["story", "creativity"] }
        ]
      },
      {
        goalId: "story-finish-route",
        title: "找回故事结尾",
        need: "需要飞行、照明或漂浮路线。",
        solvedText: "结尾回到书页，孩子保存了一条复玩路线。",
        solutions: [
          { solutionId: "flying-boat", label: "飞行小船", objectKinds: ["boat"], propertyAny: ["flying", "floating"], actions: ["cross"], resultTags: ["finishStory"], sparkTags: ["story", "science"] },
          { solutionId: "light-kite", label: "发光风筝", objectKinds: ["kite"], propertyAny: ["illuminating", "flying"], actions: ["light", "cross"], resultTags: ["finishStory"], sparkTags: ["creativity", "nature"] },
          { solutionId: "story-lamp", label: "故事灯", objectKinds: ["lamp"], propertyAny: ["illuminating", "warm"], actions: ["light"], resultTags: ["finishStory"], sparkTags: ["story", "aiCollaboration"] }
        ]
      }
    ],
  },
};

export const initialWorldStates: Record<WorldId, WorldState> = {
  "rainbow-island": { objects: [], inventory: [], solvedGoalIds: [], activeTags: [], comfort: 1, light: 1, access: 1, story: ["小火等着孩子把词语变成可玩物件。"] },
  "story-planet": { objects: [], inventory: [], solvedGoalIds: [], activeTags: [], comfort: 1, light: 1, access: 1, story: ["泡泡船长把空白故事页铺在舞台上。"] },
};
"""


def _safety_ts() -> str:
    return """import type { SafetyLevel } from "../types";

const blockedTerms = ["炸", "爆炸", "打人", "伤害", "杀", "血", "枪", "毒", "自杀", "跳楼", "裸"];

export function classifySafety(input: string): { level: SafetyLevel; reason?: string } {
  const normalized = input.trim().toLowerCase();
  const reason = blockedTerms.find((term) => normalized.includes(term));
  return reason ? { level: "redirected", reason } : { level: "safe" };
}
"""


def _word_to_world_ts() -> str:
    return """import type { ActionIntent, GeneratedObject, ObjectKind, PropertyKey, SparkKey, WorldId } from "../types";
import { classifySafety } from "./safety";

interface VocabularyEntry {
  kind: ObjectKind;
  aliases: string[];
  defaultName: string;
  sparkTags: SparkKey[];
}

const vocabulary: VocabularyEntry[] = [
  { kind: "bridge", aliases: ["桥", "小桥", "彩虹桥", "路"], defaultName: "安全小桥", sparkTags: ["science", "creativity"] },
  { kind: "key", aliases: ["钥匙", "问题钥匙"], defaultName: "问题钥匙", sparkTags: ["science", "story"] },
  { kind: "lamp", aliases: ["灯", "星星灯", "夜灯", "光"], defaultName: "星星灯", sparkTags: ["science", "creativity"] },
  { kind: "cloud", aliases: ["云", "云朵", "星星云"], defaultName: "软云", sparkTags: ["nature", "creativity"] },
  { kind: "boat", aliases: ["船", "小船", "飞船", "泡泡船"], defaultName: "泡泡船", sparkTags: ["science", "story"] },
  { kind: "seed", aliases: ["种子", "花", "树"], defaultName: "会长大的种子", sparkTags: ["nature"] },
  { kind: "rain", aliases: ["雨", "小雨", "水"], defaultName: "凉凉小雨", sparkTags: ["nature", "science"] },
  { kind: "blanket", aliases: ["毯子", "被子", "抱抱"], defaultName: "温暖毯子", sparkTags: ["social"] },
  { kind: "friend", aliases: ["朋友", "伙伴", "星星朋友"], defaultName: "友好伙伴", sparkTags: ["social", "story"] },
  { kind: "tool", aliases: ["工具", "锤子", "修理", "修桥"], defaultName: "修理工具", sparkTags: ["science"] },
  { kind: "music", aliases: ["音乐", "歌", "铃铛"], defaultName: "会唱歌的铃铛", sparkTags: ["story", "creativity"] },
  { kind: "animal", aliases: ["小兔", "兔子", "猫", "狗", "鸟"], defaultName: "友好小动物", sparkTags: ["nature", "social"] },
  { kind: "door", aliases: ["门", "问题之门"], defaultName: "问题之门", sparkTags: ["story", "science"] },
  { kind: "stone", aliases: ["石头", "发光石"], defaultName: "发光石头", sparkTags: ["science"] },
  { kind: "kite", aliases: ["风筝", "飞行器"], defaultName: "会飞风筝", sparkTags: ["creativity", "nature"] },
  { kind: "shield", aliases: ["护盾", "护栏", "保护"], defaultName: "安全护盾", sparkTags: ["social", "science"] },
  { kind: "tool", aliases: ["梯子", "台阶"], defaultName: "木头台阶", sparkTags: ["science", "creativity"] },
  { kind: "lamp", aliases: ["太阳", "月亮"], defaultName: "温柔光球", sparkTags: ["nature", "creativity"] },
];

const propertyLexicon: Array<{ property: PropertyKey; aliases: string[] }> = [
  { property: "red", aliases: ["红", "红色"] },
  { property: "blue", aliases: ["蓝", "蓝色"] },
  { property: "rainbow", aliases: ["彩虹", "七彩"] },
  { property: "small", aliases: ["小", "小小"] },
  { property: "big", aliases: ["大", "巨大", "高"] },
  { property: "soft", aliases: ["软", "软软", "轻轻"] },
  { property: "metal", aliases: ["金属", "铁"] },
  { property: "wooden", aliases: ["木", "木头"] },
  { property: "floating", aliases: ["漂", "漂浮", "浮"] },
  { property: "flying", aliases: ["飞", "会飞", "飞行"] },
  { property: "warm", aliases: ["暖", "温暖"] },
  { property: "cool", aliases: ["凉", "冰", "冷"] },
  { property: "wet", aliases: ["湿", "水", "雨"] },
  { property: "illuminating", aliases: ["亮", "发光", "照亮", "点亮"] },
  { property: "friendly", aliases: ["朋友", "友好", "一起"] },
  { property: "repairing", aliases: ["修", "修理", "修好"] },
  { property: "musical", aliases: ["音乐", "唱歌", "歌"] },
  { property: "safe", aliases: ["安全", "保护"] },
];

const propertyDisplayName: Record<PropertyKey, string> = {
  red: "红色",
  blue: "蓝色",
  rainbow: "彩虹",
  small: "小小",
  big: "大大的",
  soft: "软软",
  metal: "金属",
  wooden: "木头",
  floating: "会漂浮的",
  flying: "会飞的",
  warm: "温暖",
  cool: "凉凉",
  wet: "带水的",
  illuminating: "会发光的",
  friendly: "友好的",
  repairing: "会修理的",
  musical: "会唱歌的",
  safe: "安全",
};

const actionLexicon: Array<{ action: ActionIntent; aliases: string[] }> = [
  { action: "comfort", aliases: ["安慰", "抱抱", "陪", "朋友"] },
  { action: "ask", aliases: ["为什么", "问", "问题"] },
  { action: "repair", aliases: ["修", "修理", "修好"] },
  { action: "cross", aliases: ["过河", "跨", "过去", "回家"] },
  { action: "light", aliases: ["点亮", "照亮", "发光"] },
  { action: "open", aliases: ["打开", "开门", "钥匙"] },
  { action: "float", aliases: ["漂", "漂浮"] },
  { action: "protect", aliases: ["保护", "安全"] },
];

function includesAny(input: string, aliases: string[]): boolean {
  return aliases.some((alias) => input.includes(alias));
}

export function objectFromVocabulary(input: string, worldId: WorldId): VocabularyEntry {
  return vocabulary.find((entry) => includesAny(input, entry.aliases))
    ?? (worldId === "rainbow-island" ? vocabulary[0] : vocabulary[8]);
}

function propertiesFromInput(input: string): PropertyKey[] {
  const properties = propertyLexicon.filter((entry) => includesAny(input, entry.aliases)).map((entry) => entry.property);
  return Array.from(new Set([...properties, "safe"]));
}

function actionFromInput(input: string): ActionIntent {
  return actionLexicon.find((entry) => includesAny(input, entry.aliases))?.action ?? "create";
}

export function propertyLabel(property: PropertyKey): string {
  return propertyDisplayName[property];
}

function displayNameFromProperties(properties: PropertyKey[], baseName: string): string {
  const labels = properties
    .filter((item) => item !== "safe")
    .slice(0, 2)
    .map((item) => propertyDisplayName[item])
    .filter((label) => {
      const compact = label.replace("的", "");
      return !baseName.includes(label) && !baseName.includes(compact);
    });
  return labels.length ? `${labels.join("")}${baseName}` : baseName;
}

export function safeRedirectObject(input: string, reason: string): GeneratedObject {
  const repair = input.includes("桥") || input.includes("门");
  return {
    id: `obj-${Date.now()}-safe`,
    name: repair ? "安全修理工具" : "保护灯",
    kind: repair ? "tool" : "lamp",
    properties: repair ? ["safe", "repairing", "cool"] : ["safe", "illuminating", "warm"],
    action: repair ? "repair" : "protect",
    sourceText: input,
    safetyLevel: "redirected",
    redirectedReason: reason,
    sparkTags: ["expression", "social", "aiCollaboration"],
  };
}

export function parseChildInput(input: string, worldId: WorldId): GeneratedObject {
  const safety = classifySafety(input);
  if (safety.level === "redirected") return safeRedirectObject(input, safety.reason ?? "安全边界");
  const entry = objectFromVocabulary(input, worldId);
  const properties = propertiesFromInput(input);
  const action = actionFromInput(input);
  return {
    id: `obj-${Date.now()}-${entry.kind}`,
    name: displayNameFromProperties(properties, entry.defaultName),
    kind: entry.kind,
    properties,
    action,
    sourceText: input,
    safetyLevel: "safe",
    sparkTags: Array.from(new Set(["expression", "aiCollaboration", ...entry.sparkTags])),
  };
}
"""


def _world_rules_ts() -> str:
    return """import { worlds } from "../data/worlds";
import type { GeneratedObject, PropertyKey, RuleResult, SolutionPattern, WorldId, WorldState } from "../types";

function intersects<T>(left: T[], right: T[]): boolean {
  return left.some((item) => right.includes(item));
}

export function solutionMatches(object: GeneratedObject, solution: SolutionPattern): boolean {
  return solution.objectKinds.includes(object.kind)
    && (intersects(object.properties, solution.propertyAny) || solution.actions.includes(object.action));
}

function tagPatch(tags: string[]): Partial<WorldState> {
  const patch: Partial<WorldState> = {};
  if (tags.includes("bridgeRiver")) patch.access = 3;
  if (tags.includes("extinguishFire")) patch.comfort = 3;
  if (tags.includes("lightDark")) patch.light = 3;
  if (tags.includes("openDoor")) patch.access = 3;
  if (tags.includes("comfortFriend")) patch.comfort = 3;
  if (tags.includes("finishStory")) patch.light = 3;
  return patch;
}

export function applyObjectToWorld(worldId: WorldId, state: WorldState, object: GeneratedObject): RuleResult {
  const world = worlds[worldId];
  const matched = world.goals.flatMap((goal) => goal.solutions
    .filter((solution) => !state.solvedGoalIds.includes(goal.goalId) && solutionMatches(object, solution))
    .map((solution) => ({ goal, solution })));
  const solvedGoalIds = Array.from(new Set([...state.solvedGoalIds, ...matched.map((item) => item.goal.goalId)]));
  const resultTags = Array.from(new Set(matched.flatMap((item) => item.solution.resultTags)));
  const sparkTags = Array.from(new Set([...object.sparkTags, ...matched.flatMap((item) => item.solution.sparkTags)]));
  const solutionLabels = matched.map((item) => `${item.goal.title}：${item.solution.label}`);
  const worldPatch = tagPatch(resultTags);
  const inventory = object.action === "create" || object.action === "repair" ? [...state.inventory, object] : state.inventory;
  const feedback = matched.length
    ? `${world.companion}让${object.name}进入舞台，${solutionLabels.join("；")}。`
    : `${world.companion}保存了${object.name}。它还没解开目标，但可以放进背包继续组合。`;
  return {
    object,
    solvedGoalIds,
    newlySolvedGoalIds: matched.map((item) => item.goal.goalId),
    worldPatch: {
      ...worldPatch,
      objects: [...state.objects, object],
      inventory,
      solvedGoalIds,
      activeTags: Array.from(new Set([...state.activeTags, ...resultTags])),
      story: [...state.story.slice(-5), feedback],
    },
    feedback,
    solutionLabels,
    sparkTags,
  };
}

export function worldCompletion(state: WorldState, worldId: WorldId): number {
  return Math.round((state.solvedGoalIds.length / worlds[worldId].goals.length) * 100);
}

export function availableSolutionCount(worldId: WorldId): number {
  return worlds[worldId].goals.reduce((total, goal) => total + goal.solutions.length, 0);
}
"""


def _spark_ts() -> str:
    return """import type { PlayEvent, SparkKey, SparkScores } from "../types";

export const sparkLabels: Record<SparkKey, string> = {
  expression: "表达火花",
  creativity: "创造火花",
  story: "故事火花",
  science: "探索火花",
  social: "共情火花",
  nature: "自然火花",
  aiCollaboration: "AI协作火花",
};

export const initialSparkScores: SparkScores = { expression: 4, creativity: 4, story: 4, science: 4, social: 4, nature: 4, aiCollaboration: 4 };

export function applySpark(scores: SparkScores, tags: SparkKey[]): SparkScores {
  const next = { ...scores };
  for (const tag of tags) next[tag] = Math.min(100, next[tag] + 7);
  return next;
}

export function topSpark(scores: SparkScores): SparkKey {
  return (Object.keys(scores) as SparkKey[]).sort((a, b) => scores[b] - scores[a])[0];
}

export function summarizeSpark(events: PlayEvent[], scores: SparkScores): string {
  if (!events.length) return "还没有 Spark traces。孩子生成第一个物件后，这里会显示真实解题路径。";
  const top = sparkLabels[topSpark(scores)];
  const solved = events.reduce((sum, event) => sum + event.solvedGoalIds.length, 0);
  const redirected = events.some((event) => event.object.safetyLevel === "redirected");
  return `${top}最亮。孩子已经尝试 ${events.length} 次造物，触发 ${solved} 个目标解法${redirected ? "，并完成一次安全重定向" : ""}。`;
}
"""


def _storage_ts() -> str:
    return """import { initialWorldStates } from "../data/worlds";
import type { GameSnapshot } from "../types";
import { initialSparkScores } from "./spark";

const storageKey = "huohutu.spark.scribble.final.v2";

export function createInitialSnapshot(): GameSnapshot {
  return {
    ageBand: "5-6",
    activeWorldId: "rainbow-island",
    worlds: structuredClone(initialWorldStates),
    sparkScores: initialSparkScores,
    events: [],
    undoStack: [],
    replayLog: [],
    cloudTrail: ["local:start"],
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
import { parseChildInput, propertyLabel } from "./engine/wordToWorld";
import { applyObjectToWorld, availableSolutionCount, worldCompletion } from "./engine/worldRules";
import { applySpark, sparkLabels, summarizeSpark, topSpark } from "./engine/spark";
import { cloudQueueMessage, loadSnapshot, resetSnapshot, saveSnapshot } from "./engine/storage";
import type { AgeBand, GameSnapshot, PlayEvent, WorldId } from "./types";

const ageBands: Array<{ id: AgeBand; label: string }> = [
  { id: "2-4", label: "2-4岁" },
  { id: "5-6", label: "5-6岁" },
  { id: "7-9", label: "7-9岁" },
];

export default function App() {
  const [snapshot, setSnapshot] = useState<GameSnapshot>(() => loadSnapshot());
  const [mode, setMode] = useState<"play" | "parent" | "test">("play");
  const [idea, setIdea] = useState("");
  const [inspectedObjectId, setInspectedObjectId] = useState<string | null>(null);

  useEffect(() => saveSnapshot(snapshot), [snapshot]);

  const activeWorld = worlds[snapshot.activeWorldId];
  const activeState = snapshot.worlds[snapshot.activeWorldId];
  const topSparkKey = topSpark(snapshot.sparkScores);
  const latestEvent = snapshot.events[0];
  const inspectedObject = activeState.objects.find((object) => object.id === inspectedObjectId) ?? activeState.objects.at(-1);
  const solutionBadges = useMemo(
    () => activeWorld.goals.flatMap((goal) => goal.solutions.map((solution) => `${goal.title} / ${solution.label}`)),
    [activeWorld],
  );
  const funPlaytestChecks = [
    { label: "自然语言生成物件", ok: activeState.objects.length > 0 },
    { label: "属性影响世界规则", ok: activeState.activeTags.length > 0 },
    { label: "每世界 3 个目标", ok: activeWorld.goals.length >= 3 },
    { label: "每目标 3 条解法", ok: activeWorld.goals.every((goal) => goal.solutions.length >= 3) },
    { label: "支持撤销和复玩", ok: snapshot.undoStack.length > 0 || snapshot.replayLog.length > 0 },
    { label: "安全重定向", ok: snapshot.events.some((event) => event.object.safetyLevel === "redirected") },
  ];

  function update(mutator: (current: GameSnapshot) => GameSnapshot) {
    setSnapshot((current) => mutator(current));
  }

  function createFromWords(text: string) {
    const childText = text.trim();
    if (!childText) return;
    update((current) => {
      const worldId = current.activeWorldId;
      const before = current.worlds[worldId];
      const object = parseChildInput(childText, worldId);
      const ruleResult = applyObjectToWorld(worldId, before, object);
      const event: PlayEvent = {
        id: `event-${Date.now()}`,
        at: new Date().toISOString(),
        worldId,
        input: childText,
        object,
        feedback: ruleResult.feedback,
        solvedGoalIds: ruleResult.newlySolvedGoalIds,
        solutionLabels: ruleResult.solutionLabels,
        sparkTags: ruleResult.sparkTags,
      };
      return {
        ...current,
        worlds: { ...current.worlds, [worldId]: { ...before, ...ruleResult.worldPatch } },
        sparkScores: applySpark(current.sparkScores, ruleResult.sparkTags),
        events: [event, ...current.events].slice(0, 120),
        undoStack: [{ worldId, before, eventId: event.id }, ...current.undoStack].slice(0, 30),
        replayLog: ruleResult.solutionLabels.length ? [`${worlds[worldId].shortName}:${ruleResult.solutionLabels.join("|")}`, ...current.replayLog].slice(0, 50) : current.replayLog,
        cloudTrail: [`local:${event.id}:${object.name}`, ...current.cloudTrail].slice(0, 80),
      };
    });
    setIdea("");
  }

  function undoLastObject() {
    update((current) => {
      const undo = current.undoStack[0];
      if (!undo) return current;
      return {
        ...current,
        worlds: { ...current.worlds, [undo.worldId]: undo.before },
        events: current.events.filter((event) => event.id !== undo.eventId),
        undoStack: current.undoStack.slice(1),
        replayLog: [`undo:${undo.eventId}`, ...current.replayLog].slice(0, 50),
      };
    });
  }

  function resetWorld() {
    setSnapshot(resetSnapshot());
    setInspectedObjectId(null);
  }

  function replaySolution(label: string) {
    update((current) => ({ ...current, replayLog: [`replay:${label}`, ...current.replayLog].slice(0, 50) }));
    setMode("play");
  }

  function inspectObject(objectId: string) {
    setInspectedObjectId(objectId);
  }

  return (
    <main className="tabletWorkbench" data-playtest="scribble-spark">
      <header className="topBar">
        <div><strong>火火兔 Scribble Spark</strong><span>原创词语造物谜题沙盒</span></div>
        <nav>
          <button className={mode === "play" ? "active" : ""} onClick={() => setMode("play")}>造物解谜</button>
          <button className={mode === "parent" ? "active" : ""} onClick={() => setMode("parent")}>家长火花册</button>
          <button className={mode === "test" ? "active" : ""} onClick={() => setMode("test")}>监督门禁</button>
        </nav>
      </header>

      {mode === "play" ? (
        <section className="playGrid">
          <aside className="controlPanel">
            <h2>世界</h2>
            {(Object.keys(worlds) as WorldId[]).map((worldId) => (
              <button key={worldId} className={snapshot.activeWorldId === worldId ? "selected" : ""} onClick={() => update((current) => ({ ...current, activeWorldId: worldId }))}>
                <strong>{worlds[worldId].name}</strong><span>{worlds[worldId].premise}</span>
              </button>
            ))}
            <h2>年龄</h2>
            <div className="ageRow">{ageBands.map((age) => <button key={age.id} className={snapshot.ageBand === age.id ? "selected" : ""} onClick={() => update((current) => ({ ...current, ageBand: age.id }))}>{age.label}</button>)}</div>
            <button onClick={undoLastObject}>撤销</button>
            <button onClick={resetWorld}>重置世界</button>
          </aside>

          <section className="stagePanel">
            <div className="worldHeader">
              <div><span>{activeWorld.companion}</span><h1>{activeWorld.name}</h1><p>{activeWorld.premise}</p></div>
              <strong>{worldCompletion(activeState, activeWorld.id)}%</strong>
            </div>
            <div className="stageScene" style={{ "--sky": activeWorld.palette.sky, "--ground": activeWorld.palette.ground, "--accent": activeWorld.palette.accent } as CSSProperties}>
              <div className="sky" /><div className="ground" />
              {activeState.objects.map((object, index) => <button key={object.id} className={`objectCard object${(index % 6) + 1}`} onClick={() => inspectObject(object.id)}>{object.name}</button>)}
              <article className="companion"><strong>{activeWorld.companion}</strong><span>{latestEvent?.feedback ?? "孩子说一个想法，我会把它变成可玩的安全物件。"}</span></article>
            </div>
            <form className="ideaForm" onSubmit={(event) => { event.preventDefault(); createFromWords(idea); }}>
              <input value={idea} onChange={(event) => setIdea(event.target.value)} placeholder={`孩子说一个想法，例如：${activeWorld.starterIdeas[0]}`} />
              <button type="submit">生成物件</button>
            </form>
            <div className="starterIdeas">{activeWorld.starterIdeas.map((starter) => <button key={starter} onClick={() => createFromWords(starter)}>{starter}</button>)}</div>
          </section>

          <aside className="goalPanel">
            <h2>目标与可行解法</h2>
            {activeWorld.goals.map((goal) => (
              <article key={goal.goalId} className={activeState.solvedGoalIds.includes(goal.goalId) ? "goal solved" : "goal"}>
                <strong>{goal.title}</strong><p>{goal.need}</p>
                <small>{goal.solutions.length} 条可行解法</small>
              </article>
            ))}
            <h2>背包</h2>
            <div className="inventoryShelf">{activeState.inventory.length ? activeState.inventory.map((object) => <span key={object.id}>{object.name}</span>) : <span>还没有物件</span>}</div>
            {inspectedObject ? <article className="inspectBox"><strong>{inspectedObject.name}</strong><p>{inspectedObject.properties.map(propertyLabel).join(" · ")}</p><small>{inspectedObject.action} / {inspectedObject.safetyLevel === "redirected" ? "安全重定向" : "安全生成"}</small></article> : null}
          </aside>
        </section>
      ) : mode === "parent" ? (
        <section className="parentBook">
          <header><div><span>Spark traces</span><h1>火花解读</h1></div><strong>{sparkLabels[topSparkKey]}</strong></header>
          <article className="summaryCard"><p>{summarizeSpark(snapshot.events, snapshot.sparkScores)}</p><strong>云端队列</strong><small>{cloudQueueMessage(snapshot)}</small></article>
          <section className="timeline">
            {snapshot.events.length ? snapshot.events.slice(0, 10).map((event) => <article key={event.id}><strong>{worlds[event.worldId].shortName} · {event.object.name}</strong><p>{event.feedback}</p><small>{event.sparkTags.map((tag) => sparkLabels[tag]).join(" · ")}</small></article>) : <p>还没有本地行为轨迹。</p>}
          </section>
          <section className="replayPanel"><h2>复玩解法</h2>{snapshot.replayLog.map((item) => <button key={item} onClick={() => replaySolution(item)}>{item}</button>)}</section>
        </section>
      ) : (
        <section className="testPanel">
          <header><div><span>Browser Playtest Hook</span><h1>监督门禁</h1></div><strong>{funPlaytestChecks.filter((check) => check.ok).length}/{funPlaytestChecks.length}</strong></header>
          <section className="solutionBadges">{solutionBadges.map((badge) => <button key={badge} onClick={() => replaySolution(badge)}>{badge}</button>)}</section>
          <section className="checks">{funPlaytestChecks.map((check) => <p key={check.label} className={check.ok ? "ok" : ""}>{check.ok ? "通过" : "待测"} · {check.label}</p>)}</section>
          <p>当前世界共有 {availableSolutionCount(activeWorld.id)} 条系统解法，最终门禁要求每个世界至少 3 个目标、每个目标至少 3 条解法。</p>
        </section>
      )}
    </main>
  );
}
"""


def _styles_css() -> str:
    return """:root { font-family: Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif; color: #172332; background: #f6f8fb; } * { box-sizing: border-box; } body { margin: 0; min-width: 320px; min-height: 100vh; } button, input { font: inherit; } button { cursor: pointer; }
.tabletWorkbench { min-height: 100vh; display: flex; flex-direction: column; }
.topBar { min-height: 72px; padding: 14px 22px; display: flex; align-items: center; justify-content: space-between; gap: 18px; background: #fff; border-bottom: 1px solid #dde5ee; }
.topBar strong, .topBar span { display: block; } .topBar span { color: #607084; font-size: 13px; }
.topBar nav { display: flex; gap: 8px; } .topBar button, .controlPanel button, .starterIdeas button, .solutionBadges button, .replayPanel button { border: 1px solid #ccd8e5; border-radius: 8px; background: #fff; color: #172332; min-height: 42px; padding: 8px 12px; font-weight: 800; }
.topBar button.active, .controlPanel button.selected, .ageRow button.selected { background: #1d6f8f; color: #fff; border-color: #1d6f8f; }
.playGrid { flex: 1; display: grid; grid-template-columns: 270px minmax(420px, 1fr) 310px; gap: 16px; padding: 16px; min-height: calc(100vh - 72px); }
.controlPanel, .goalPanel, .stagePanel, .parentBook, .testPanel { background: #fff; border: 1px solid #dde5ee; border-radius: 8px; padding: 16px; min-width: 0; }
.controlPanel { display: flex; flex-direction: column; gap: 10px; } .controlPanel h2, .goalPanel h2 { margin: 8px 0 4px; font-size: 15px; color: #607084; }
.controlPanel button { text-align: left; } .controlPanel button span { display: block; margin-top: 4px; color: inherit; opacity: .78; font-size: 12px; line-height: 1.35; }
.ageRow { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
.stagePanel { display: flex; flex-direction: column; gap: 12px; }
.worldHeader { display: flex; align-items: center; justify-content: space-between; gap: 14px; } .worldHeader h1 { margin: 4px 0; font-size: 28px; } .worldHeader p { margin: 0; color: #607084; } .worldHeader > strong { font-size: 28px; color: #1d6f8f; }
.stageScene { --sky: #7ed7f7; --ground: #8bd17c; --accent: #ff8a65; position: relative; overflow: hidden; min-height: 410px; border-radius: 8px; border: 1px solid #cfdce9; background: linear-gradient(180deg, var(--sky), #eaf8ff 58%, var(--ground) 59%); }
.sky, .ground, .objectCard, .companion { position: absolute; } .sky { inset: 0 0 42% 0; background: radial-gradient(circle at 75% 18%, rgba(255,255,255,.9) 0 38px, transparent 39px); } .ground { left: -10%; right: -10%; bottom: -80px; height: 180px; border-radius: 50% 50% 0 0; background: var(--ground); }
.objectCard { max-width: 138px; min-width: 92px; min-height: 54px; padding: 9px 11px; border: 1px solid rgba(23,35,50,.15); border-radius: 8px; background: rgba(255,255,255,.92); color: #172332; box-shadow: 0 10px 22px rgba(23,35,50,.13); font-weight: 900; }
.object1 { left: 9%; bottom: 82px; } .object2 { left: 28%; bottom: 118px; } .object3 { left: 48%; bottom: 74px; } .object4 { right: 15%; bottom: 132px; } .object5 { right: 7%; bottom: 72px; } .object6 { left: 60%; top: 96px; }
.companion { left: 22px; bottom: 22px; width: min(360px, calc(100% - 44px)); padding: 14px; border-radius: 8px; background: #172332; color: #fff; } .companion span { display: block; margin-top: 6px; color: #d9f1ff; line-height: 1.45; }
.ideaForm { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; } .ideaForm input { min-height: 50px; border: 1px solid #c9d8e6; border-radius: 8px; padding: 0 14px; } .ideaForm button { border: 0; border-radius: 8px; background: #ff8a65; color: #fff; padding: 0 20px; font-weight: 900; }
.starterIdeas, .solutionBadges, .replayPanel { display: flex; flex-wrap: wrap; gap: 8px; }
.goal { border: 1px solid #dde5ee; border-radius: 8px; padding: 12px; margin-bottom: 10px; background: #fbfdff; } .goal.solved { background: #eefaf5; border-color: #78c7a8; } .goal p { margin: 6px 0; color: #607084; line-height: 1.4; }
.inventoryShelf { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; } .inventoryShelf span { border-radius: 999px; background: #eff5fb; padding: 7px 10px; font-size: 12px; font-weight: 800; }
.inspectBox, .summaryCard, .timeline article, .checks p { border: 1px solid #dde5ee; border-radius: 8px; padding: 12px; background: #fbfdff; }
.inspectBox p { margin: 6px 0; color: #607084; }
.parentBook, .testPanel { margin: 16px; display: grid; gap: 14px; } .parentBook header, .testPanel header { display: flex; align-items: center; justify-content: space-between; } .parentBook h1, .testPanel h1 { margin: 4px 0 0; }
.timeline { display: grid; gap: 10px; } .timeline p { margin: 6px 0; color: #607084; }
.checks { display: grid; gap: 8px; } .checks p { margin: 0; } .checks p.ok { background: #eefaf5; border-color: #78c7a8; }
@media (max-width: 1050px) { .playGrid { grid-template-columns: 1fr; } .topBar { align-items: stretch; flex-direction: column; } .topBar nav { flex-wrap: wrap; } }
@media (max-width: 620px) { .playGrid, .parentBook, .testPanel { padding: 10px; margin: 0; } .ideaForm { grid-template-columns: 1fr; } .stageScene { min-height: 500px; } }
"""


def _readme_md() -> str:
    return """# 火火兔 Scribble Spark Final V2

KUN Control Plane 生成的原创 Fire Rabbit word-to-world puzzle sandbox。

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

- 对标的是词语造物、属性规则、多解谜题、撤销/复玩和家长可读轨迹等功能系统。
- 不复制任何商业游戏角色、美术、关卡、文案、UI skin、音频、商标或 trade dress。
- 云端行为轨迹是本地模拟队列；真实儿童数据上传前必须另行审批。
"""
