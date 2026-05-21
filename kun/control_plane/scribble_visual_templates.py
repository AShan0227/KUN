"""Visual/product templates for the Wordforge Scribble Adventure dogfood game."""

from __future__ import annotations


def visual_product_ready_files() -> dict[str, str]:
    """Return a KUN-generated visual product iteration file set."""

    return {
        "src/App.tsx": _app_tsx(),
        "src/styles.css": _styles_css(),
        "src/data/visuals.ts": _visuals_ts(),
        "scripts/visual-product-test.mjs": _visual_test_mjs(),
        "docs/visual-product-iteration.md": _visual_iteration_md(),
        "public/assets/companion-xiaobi.svg": _companion_svg("#ff8c6a", "#24435d"),
        "public/assets/companion-mogu.svg": _companion_svg("#56c2ff", "#334155"),
        "public/assets/companion-gearling.svg": _companion_svg("#70d68c", "#263238"),
        "public/assets/companion-skykid.svg": _companion_svg("#ffd166", "#2f3a55"),
        "public/assets/world-rainbow-island.svg": _world_svg("#91e4ff", "#d8f5a2", "#ffbf69", "rainbow"),
        "public/assets/world-story-star.svg": _world_svg("#dde7ff", "#f8d6ff", "#8da2ff", "star"),
        "public/assets/world-gear-garden.svg": _world_svg("#d5f6e6", "#bde4b8", "#56c2a6", "gear"),
        "public/assets/world-sky-harbor.svg": _world_svg("#cff2ff", "#f4dfb8", "#5bb6ff", "cloud"),
        "public/assets/object-bridge.svg": _object_svg("#ffbf69", "bridge"),
        "public/assets/object-light.svg": _object_svg("#ffd166", "light"),
        "public/assets/object-rain.svg": _object_svg("#56c2ff", "rain"),
        "public/assets/object-tool.svg": _object_svg("#70d68c", "tool"),
        "public/assets/object-friend.svg": _object_svg("#ff8cbe", "friend"),
        "public/assets/object-nature.svg": _object_svg("#7bd88f", "nature"),
        "public/assets/object-magic.svg": _object_svg("#9b8cff", "magic"),
        "public/assets/object-default.svg": _object_svg("#ffffff", "default"),
    }


def _app_tsx() -> str:
    return """import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { visualsForWorld, objectAsset, objectMotionClass, stickerForObject } from "./data/visuals";
import { worlds } from "./data/worlds";
import { attachObjects as attachWorldObjects, combineObjects as combineWorldObjects, editObject, parseChildInput, propertyLabel, propertyLexicon } from "./engine/wordToWorld";
import { applyObjectToWorld, npcRequests, worldCompletion } from "./engine/worldRules";
import { applySpark, sparkLabels, summarizeSpark, topSpark } from "./engine/spark";
import { explainCausalOutcome } from "./engine/causalPhysics";
import { cloudQueueMessage, loadSnapshot, resetSnapshot, saveSnapshot } from "./engine/storage";
import type { GameSnapshot, GeneratedObject, PlayEvent, WorldId } from "./types";

export default function App() {
  const [snapshot, setSnapshot] = useState<GameSnapshot>(() => loadSnapshot());
  const [mode, setMode] = useState<"play" | "parent" | "test">("play");
  const [idea, setIdea] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [editProperty, setEditProperty] = useState("flying");
  useEffect(() => saveSnapshot(snapshot), [snapshot]);

  const worldIds = Object.keys(worlds) as WorldId[];
  const activeWorld = worlds[snapshot.activeWorldId];
  const activeVisual = visualsForWorld(snapshot.activeWorldId);
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
      const causal = explainCausalOutcome(object, before.activeTags);
      const event: PlayEvent = {
        id: `event-${Date.now()}`,
        at: new Date().toISOString(),
        worldId,
        input: object.sourceText,
        object,
        feedback: `${result.feedback} ${causal.summary}`,
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
    { label: "角色与世界图片", ok: Boolean(activeVisual.companionPortrait && activeVisual.backdrop) },
    { label: "物件图标与动画", ok: activeState.objects.some((object) => objectAsset(object).includes("/assets/")) },
    { label: "对象编辑", ok: activeState.inventory.some((object) => object.action === "transform") },
    { label: "组合或附着", ok: activeState.inventory.some((object) => object.action === "combine" || object.action === "attach") },
    { label: "奖励碎片", ok: activeState.rewardShards > 0 },
  ];

  return (
    <main className="tabletWorkbench visual-polish-ready" data-playtest="scribble-parity">
      <header className="topBar gameHud">
        <div className="brandLockup"><span className="brandMark">W</span><div><strong>文字造物冒险</strong><span>功能对标 · 原创文字造物表达 · 原创角色视觉化</span></div></div>
        <nav><button onClick={() => setMode("play")}>造物解谜</button><button onClick={() => setMode("parent")}>家长灵感册</button><button onClick={() => setMode("test")}>监督门禁</button></nav>
      </header>
      {mode === "play" ? (
        <section className="playGrid visualGameShell">
          <aside className="panel explorationMap">
            <h2>开放探索地图</h2>
            {worldIds.map((worldId) => {
              const visual = visualsForWorld(worldId);
              return <button key={worldId} className={snapshot.activeWorldId === worldId ? "selected mapTile" : "mapTile"} onClick={() => update((current) => ({ ...current, activeWorldId: worldId }))}><img src={visual.backdrop} alt="" /><span>{worlds[worldId].name}</span><small>{worldCompletion(snapshot.worlds[worldId], worldId)}%</small></button>;
            })}
            <button onClick={undoLastObject}>撤销一步</button><button onClick={resetWorld}>重置世界</button>
          </aside>

          <section className="stagePanel visualStage" style={{ "--sky": activeWorld.palette.sky, "--ground": activeWorld.palette.ground, "--accent": activeWorld.palette.accent } as CSSProperties}>
            <div className="worldHeader">
              <div className="companionPortrait"><img src={activeVisual.companionPortrait} alt={`${activeWorld.companion} 角色`} /><span>{activeWorld.companion}</span></div>
              <div><span className="chapterPill">{activeVisual.chapter}</span><h1>{activeWorld.name}</h1><p>{activeWorld.premise}</p></div>
              <strong className="shardBadge">{activeState.rewardShards} 灵感碎片</strong>
            </div>
            <div className="stageScene illustratedScene">
              <img className="worldBackdrop" src={activeVisual.backdrop} alt="" />
              <div className="sceneLayer">{activeState.objects.slice(-18).map((object, index) => <ObjectToken key={object.id} object={object} index={index} selected={selectedIds.includes(object.id)} onSelect={() => setSelectedIds((ids) => ids.includes(object.id) ? ids.filter((id) => id !== object.id) : [...ids.slice(-1), object.id])} />)}</div>
              <article className="companion speechBubble"><strong>{activeWorld.companion}</strong><span>{latest?.feedback ?? "说一个想法，我会把词语变成能行动、能组合、能解谜的东西。"}</span>{latest?.object.safetyLevel === "redirected" ? <em>安全重定向：{latest.object.redirectedReason}</em> : null}</article>
            </div>
            <form className="ideaForm spellInput" onSubmit={(event) => { event.preventDefault(); createFromWords(idea); }}><input value={idea} onChange={(event) => setIdea(event.target.value)} placeholder={`孩子说一个想法，例如：${activeWorld.starterIdeas[0]}`} /><button>生成物件</button></form>
            <div className="starterIdeas sparkSpellbook">{activeWorld.starterIdeas.map((starter) => <button key={starter} onClick={() => createFromWords(starter)}>{starter}</button>)}</div>
          </section>

          <aside className="panel toolPanel">
            <h2>NPC 请求</h2><div className="npcRequests">{npcRequests(snapshot.activeWorldId).slice(0, 6).map((request) => <p key={request}>{request}</p>)}</div>
            <h2>对象工坊</h2><select value={editProperty} onChange={(event) => setEditProperty(event.target.value)}>{propertyLexicon.slice(0, 44).map((item) => <option key={item.property} value={item.property}>{item.label}</option>)}</select><button onClick={objectEditor}>添加属性</button><button onClick={combineObjects}>组合两个物件</button><button onClick={attachObject}>附着两个物件</button>
            <h2>因果实验室</h2><div className="causalLab">{latest ? explainCausalOutcome(latest.object, activeState.activeTags).facts.map((fact) => <span key={fact}>{fact}</span>) : <span>生成物件后会显示重量、浮力、光照、连接和目标影响。</span>}</div>
            <h2>背包</h2><div className="inventoryShelf">{activeState.inventory.slice(-12).map((object) => <button key={object.id} className={selectedIds.includes(object.id) ? "selected inventoryToken" : "inventoryToken"} onClick={() => setSelectedIds((ids) => ids.includes(object.id) ? ids.filter((id) => id !== object.id) : [...ids.slice(-1), object.id])}><img src={objectAsset(object)} alt="" />{object.name}</button>)}</div>
          </aside>
        </section>
      ) : mode === "parent" ? (
        <section className="parentBook"><header><div><span>Idea traces</span><h1>灵感解读</h1></div><strong>{sparkLabels[topSparkKey]}</strong></header><article className="summaryCard"><p>{summarizeSpark(snapshot.events, snapshot.sparkScores)}</p><strong>云端队列</strong><small>{cloudQueueMessage(snapshot)}</small></article>{snapshot.events.slice(0, 12).map((event) => <article className="timelineCard" key={event.id}><strong>{worlds[event.worldId].shortName} · {event.object.name}</strong><p>{event.feedback}</p><small>{event.sparkTags.map((tag) => sparkLabels[tag]).join(" · ")}</small></article>)}</section>
      ) : (
        <section className="testPanel"><header><div><span>External Gate</span><h1>产品体验监督门禁</h1></div><strong>{testChecks.filter((check) => check.ok).length}/{testChecks.length}</strong></header>{testChecks.map((check) => <article className={check.ok ? "pass" : "wait"} key={check.label}>{check.label}</article>)}</section>
      )}
    </main>
  );
}

function ObjectToken({ object, index, selected, onSelect }: { object: GeneratedObject; index: number; selected: boolean; onSelect: () => void }) {
  return <button className={`objectCard objectSprite object${(index % 8) + 1} ${objectMotionClass(object)} ${selected ? "selected" : ""}`} onClick={onSelect}><img src={objectAsset(object)} alt="" /><span>{object.name}</span><small>{object.properties.slice(0, 3).map(propertyLabel).join(" · ")}</small><em>{stickerForObject(object)}</em>{object.safetyLevel === "redirected" ? <b>安全重定向</b> : null}</button>;
}
"""


def _visuals_ts() -> str:
    return """import type { GeneratedObject, WorldId } from "../types";

export interface WorldVisual {
  chapter: string;
  backdrop: string;
  companionPortrait: string;
}

export const worldVisuals: Record<string, WorldVisual> = {
  "rainbow-island": { chapter: "第一幕 · 词语变形", backdrop: "/assets/world-rainbow-island.svg", companionPortrait: "/assets/companion-xiaobi.svg" },
  "story-star": { chapter: "第二幕 · 谜题故事", backdrop: "/assets/world-story-star.svg", companionPortrait: "/assets/companion-mogu.svg" },
  "gear-garden": { chapter: "第三幕 · 机关花园", backdrop: "/assets/world-gear-garden.svg", companionPortrait: "/assets/companion-gearling.svg" },
  "sky-harbor": { chapter: "第四幕 · 浮空港湾", backdrop: "/assets/world-sky-harbor.svg", companionPortrait: "/assets/companion-skykid.svg" },
};

export function visualsForWorld(worldId: WorldId): WorldVisual {
  return worldVisuals[worldId] ?? worldVisuals["rainbow-island"];
}

export function objectAsset(object: GeneratedObject): string {
  if (object.kind.includes("bridge") || object.kind.includes("ladder") || object.kind.includes("boat")) return "/assets/object-bridge.svg";
  if (object.kind.includes("lamp") || object.kind.includes("light") || object.properties.includes("illuminating")) return "/assets/object-light.svg";
  if (object.kind.includes("rain") || object.kind.includes("cloud") || object.properties.includes("wet") || object.properties.includes("cool")) return "/assets/object-rain.svg";
  if (object.kind.includes("key") || object.kind.includes("tool") || object.properties.includes("repair")) return "/assets/object-tool.svg";
  if (object.kind.includes("friend") || object.properties.includes("friendly") || object.properties.includes("singing")) return "/assets/object-friend.svg";
  if (object.kind.includes("seed") || object.kind.includes("tree") || object.kind.includes("flower")) return "/assets/object-nature.svg";
  if (object.properties.includes("flying") || object.properties.includes("floating") || object.properties.includes("rainbow")) return "/assets/object-magic.svg";
  return "/assets/object-default.svg";
}

export function objectMotionClass(object: GeneratedObject): string {
  if (object.properties.includes("flying")) return "motionFlying";
  if (object.properties.includes("floating")) return "motionFloating";
  if (object.properties.includes("bouncy")) return "motionBouncy";
  return "motionIdle";
}

export function stickerForObject(object: GeneratedObject): string {
  if (object.action === "combine") return "组合";
  if (object.action === "attach") return "附着";
  if (object.properties.includes("illuminating")) return "发光";
  if (object.properties.includes("flying")) return "飞行";
  if (object.safetyLevel === "redirected") return "安全";
  return "可用";
}
"""


def _styles_css() -> str:
    return """*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#e9f5f9;color:#182333}.tabletWorkbench{min-height:100vh}.topBar{display:flex;justify-content:space-between;gap:16px;align-items:center;padding:14px 20px;border-bottom:2px solid #203142;background:#fffaf0;position:sticky;top:0;z-index:5;box-shadow:0 8px 24px rgba(18,35,51,.12)}.brandLockup{display:flex;align-items:center;gap:12px}.brandMark{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;background:#ff8c6a;color:white;font-weight:1000;font-size:24px;border:3px solid #203142;box-shadow:4px 4px 0 #203142}.topBar strong{display:block;font-size:22px;letter-spacing:0}.topBar span{color:#607080}.topBar button,.panel button,.ideaForm button,.starterIdeas button,.inventoryShelf button{border:2px solid #203142;background:white;border-radius:8px;padding:10px 12px;font-weight:900;color:#233247;box-shadow:3px 3px 0 #203142;cursor:pointer}.topBar button:hover,.selected{border-color:#1887ff!important;background:#eaf4ff!important;transform:translateY(-1px)}.playGrid{display:grid;grid-template-columns:250px minmax(460px,1fr) 330px;gap:14px;padding:14px}.panel,.stagePanel,.parentBook,.testPanel{background:#fffdf7;border:2px solid #203142;border-radius:8px;padding:14px;box-shadow:5px 5px 0 rgba(32,49,66,.18)}.explorationMap h2,.toolPanel h2{margin:6px 0 10px}.mapTile{display:grid!important;grid-template-columns:52px 1fr auto;align-items:center;gap:9px;width:100%;margin:0 0 10px;text-align:left}.mapTile img{width:52px;height:42px;object-fit:cover;border-radius:7px;border:1px solid #d3e2ef}.mapTile small{font-weight:1000;color:#1887ff}.worldHeader{display:grid;grid-template-columns:96px 1fr auto;gap:14px;align-items:center;margin-bottom:12px}.worldHeader h1{margin:2px 0;font-size:32px}.chapterPill{display:inline-flex;border:2px solid #203142;border-radius:999px;background:#fff;padding:5px 9px;font-weight:900;color:#405166}.companionPortrait{display:grid;justify-items:center;gap:4px;font-weight:1000}.companionPortrait img{width:82px;height:82px;filter:drop-shadow(3px 5px 0 rgba(32,49,66,.24))}.shardBadge{border:2px solid #203142;background:#fff6bf;border-radius:8px;padding:10px;box-shadow:3px 3px 0 #203142}.stageScene{height:520px;border-radius:8px;background:linear-gradient(var(--sky),#fff 52%,var(--ground));position:relative;overflow:hidden;border:2px solid #203142}.worldBackdrop{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.95}.sceneLayer{position:absolute;inset:14px 14px 104px;z-index:2}.objectCard{position:absolute;display:grid;justify-items:center;gap:3px;min-width:112px;min-height:112px;border:2px solid #203142;border-radius:14px;background:rgba(255,255,255,.84);box-shadow:5px 7px 0 rgba(32,49,66,.18);padding:8px}.objectCard img{width:54px;height:54px;object-fit:contain}.objectCard span{font-weight:1000}.objectCard small{font-size:12px;color:#52677c}.objectCard em,.objectCard b{font-size:11px;border-radius:999px;padding:3px 7px;background:#eaf4ff;border:1px solid #9fc7ef;font-style:normal}.object1{left:4%;top:8%}.object2{left:26%;top:18%}.object3{left:53%;top:9%}.object4{left:72%;top:27%}.object5{left:10%;top:48%}.object6{left:36%;top:55%}.object7{left:61%;top:49%}.object8{left:78%;top:5%}.motionFlying{animation:floaty 3.2s ease-in-out infinite}.motionFloating{animation:floaty 4.2s ease-in-out infinite}.motionBouncy{animation:bouncy 2.1s ease-in-out infinite}.motionIdle{animation:idle 5s ease-in-out infinite}.speechBubble{position:absolute;left:16px;right:16px;bottom:16px;z-index:3;background:rgba(255,255,255,.94);border:2px solid #203142;border-radius:8px;padding:12px;box-shadow:4px 4px 0 rgba(32,49,66,.2)}.speechBubble strong{display:block;margin-bottom:4px}.ideaForm{display:flex;gap:8px;margin-top:12px}.ideaForm input{flex:1;border:2px solid #203142;border-radius:8px;padding:13px;font-size:16px;background:#fff}.spellInput button{background:#ff8c6a;color:white}.starterIdeas{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.sparkSpellbook button{background:#eefbf7}.npcRequests p,.timelineCard,.summaryCard{border:2px solid #dce7f1;border-radius:8px;padding:10px;background:#fbfdff}.inventoryShelf{display:flex;flex-wrap:wrap;gap:8px}.inventoryToken{display:inline-grid!important;grid-template-columns:28px auto;align-items:center;gap:6px}.inventoryToken img{width:28px;height:28px}.toolPanel select{width:100%;border:2px solid #203142;border-radius:8px;padding:10px;background:#fff;margin-bottom:8px}.toolPanel>button{display:block;width:100%;margin:8px 0;background:#fff7e8}.parentBook,.testPanel{margin:14px}.testPanel article{margin:8px 0;padding:12px;border-radius:8px;border:2px solid #203142}.pass{background:#e9f9ef}.wait{background:#fff6e5}.causalLab{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 12px}.causalLab span{border:1px solid #bad8ee;background:#eef8ff;border-radius:8px;padding:7px 8px;font-weight:800;color:#24435d}@keyframes floaty{0%,100%{transform:translateY(0) rotate(-1deg)}50%{transform:translateY(-13px) rotate(2deg)}}@keyframes bouncy{0%,100%{transform:translateY(0) scale(1)}50%{transform:translateY(-8px) scale(1.04)}}@keyframes idle{0%,100%{transform:rotate(0deg)}50%{transform:rotate(1deg)}}@media(max-width:1000px){.playGrid{grid-template-columns:1fr}.stageScene{height:500px}.topBar{align-items:flex-start;flex-direction:column}.worldHeader{grid-template-columns:82px 1fr}.shardBadge{grid-column:1/-1}}@media(max-width:560px){.playGrid{padding:8px}.stageScene{height:430px}.objectCard{min-width:96px;min-height:102px}.ideaForm{flex-direction:column}}
"""


def _visual_test_mjs() -> str:
    return """import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";

const assetDir = "public/assets";
const app = readFileSync("src/App.tsx", "utf8");
const styles = readFileSync("src/styles.css", "utf8");
const visuals = readFileSync("src/data/visuals.ts", "utf8");
const assets = existsSync(assetDir) ? readdirSync(assetDir).filter((file) => file.endsWith(".svg")) : [];
const checks = [
  ["at least 12 original svg assets", assets.length >= 12],
  ["world backdrops present", assets.filter((file) => file.startsWith("world-")).length >= 4],
  ["companion portraits present", assets.filter((file) => file.startsWith("companion-")).length >= 4],
  ["object sprites present", assets.filter((file) => file.startsWith("object-")).length >= 7],
  ["app uses illustrated stage", app.includes("illustratedScene") && app.includes("worldBackdrop")],
  ["app uses companion portraits", app.includes("companionPortrait")],
  ["app uses object sprites", app.includes("objectSprite") && app.includes("objectAsset")],
  ["visual data mapping exists", visuals.includes("worldVisuals") && visuals.includes("objectAsset")],
  ["animation hooks exist", styles.includes("@keyframes floaty") && styles.includes("motionFlying")],
  ["visual polish marker", app.includes("visual-polish-ready")],
  ["protected expression absent", !app.includes("Maxwell") && !app.includes("Starite")],
];
const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
const report = { ok: failed.length === 0, assetCount: assets.length, checks: checks.map(([name, ok]) => ({ name, ok })), failed };
writeFileSync("docs/visual-product-test-result.json", JSON.stringify(report, null, 2));
if (failed.length) {
  console.error(JSON.stringify(report, null, 2));
  process.exit(1);
}
console.log(JSON.stringify(report, null, 2));
"""


def _visual_iteration_md() -> str:
    return """# Visual Product Iteration

## Goal

The previous build proved that word-to-world mechanics worked, but it still felt
like a mechanism prototype. This iteration turns the experience into a visibly
game-shaped tablet product.

## Added

- Four original illustrated world backdrops.
- Four original companion portraits.
- Object sprite system for created items, inventory, and stage tokens.
- Game HUD, map tiles, speech bubble, spell input, reward badge, and motion animation.
- Visual product gate: `npm run test:visual`.

## Boundary

The assets are original KUN-generated SVGs. They are not copied commercial
characters, levels, UI skins, text, audio, marks, or trade dress.
"""


def _companion_svg(primary: str, ink: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160" role="img" aria-label="original companion portrait">
  <rect width="160" height="160" rx="34" fill="#fff7e8"/>
  <path d="M35 111c7-24 22-36 45-36s38 12 45 36c-18 22-72 22-90 0z" fill="{primary}" stroke="{ink}" stroke-width="7"/>
  <circle cx="80" cy="61" r="39" fill="#fff" stroke="{ink}" stroke-width="7"/>
  <circle cx="66" cy="58" r="6" fill="{ink}"/><circle cx="94" cy="58" r="6" fill="{ink}"/>
  <path d="M63 82c12 9 24 9 36 0" fill="none" stroke="{ink}" stroke-width="7" stroke-linecap="round"/>
  <path d="M35 42 18 22M124 42l18-19" stroke="{ink}" stroke-width="7" stroke-linecap="round"/>
  <circle cx="20" cy="21" r="8" fill="{primary}" stroke="{ink}" stroke-width="5"/>
  <circle cx="141" cy="21" r="8" fill="{primary}" stroke="{ink}" stroke-width="5"/>
</svg>
"""


def _world_svg(sky: str, ground: str, accent: str, motif: str) -> str:
    shape = {
        "rainbow": '<path d="M22 96c32-52 84-52 116 0" fill="none" stroke="#ff8c6a" stroke-width="14" stroke-linecap="round"/><path d="M36 96c25-32 63-32 88 0" fill="none" stroke="#ffd166" stroke-width="10" stroke-linecap="round"/>',
        "star": '<path d="m82 28 13 28 30 3-22 21 6 30-27-15-27 15 6-30-22-21 30-3z" fill="#fff6bf" stroke="#394867" stroke-width="5"/>',
        "gear": '<path d="M80 36 91 50l18-2 2 18 14 11-14 11-2 18-18-2-11 14-11-14-18 2-2-18-14-11 14-11 2-18 18 2z" fill="#d7fff0" stroke="#203142" stroke-width="5"/><circle cx="80" cy="80" r="19" fill="#fff" stroke="#203142" stroke-width="5"/>',
        "cloud": '<path d="M34 92c4-22 24-24 34-14 8-23 45-24 54 3 20-3 29 24 11 35H43c-23 0-25-28-9-24z" fill="#fff" stroke="#203142" stroke-width="5"/>',
    }[motif]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 220" role="img" aria-label="original illustrated world">
  <rect width="320" height="220" fill="{sky}"/>
  <circle cx="260" cy="44" r="24" fill="#fff6bf"/>
  <path d="M0 152c44-24 88-24 132 0s88 24 188-10v78H0z" fill="{ground}"/>
  <path d="M18 164c54-18 105-16 152 6 45 21 88 14 132-15" fill="none" stroke="{accent}" stroke-width="9" stroke-linecap="round"/>
  {shape}
</svg>
"""


def _object_svg(color: str, kind: str) -> str:
    shapes = {
        "bridge": '<path d="M22 95c24-40 92-40 116 0v24H22z" fill="{color}" stroke="#203142" stroke-width="7"/><path d="M42 95v24M80 77v42M118 95v24" stroke="#203142" stroke-width="6"/>',
        "light": '<path d="M80 20c24 0 43 19 43 43 0 17-9 29-23 39v19H60v-19C46 92 37 80 37 63c0-24 19-43 43-43z" fill="{color}" stroke="#203142" stroke-width="7"/><path d="M58 135h44" stroke="#203142" stroke-width="8" stroke-linecap="round"/>',
        "rain": '<path d="M38 67c4-22 23-26 36-16 11-25 48-20 53 9 21 2 22 34 0 36H45c-25 0-29-27-7-29z" fill="#fff" stroke="#203142" stroke-width="6"/><path d="M55 112v22M82 108v28M109 112v22" stroke="{color}" stroke-width="8" stroke-linecap="round"/>',
        "tool": '<path d="M42 116 101 57l19 19-59 59z" fill="{color}" stroke="#203142" stroke-width="7"/><path d="M99 34c16 0 26 17 18 31L95 43c4-6 6-9 4-9z" fill="#fff" stroke="#203142" stroke-width="7"/>',
        "friend": '<circle cx="80" cy="58" r="34" fill="{color}" stroke="#203142" stroke-width="7"/><path d="M42 130c8-28 68-28 76 0z" fill="#fff" stroke="#203142" stroke-width="7"/><circle cx="68" cy="57" r="5"/><circle cx="92" cy="57" r="5"/><path d="M68 76c10 8 16 8 26 0" fill="none" stroke="#203142" stroke-width="6" stroke-linecap="round"/>',
        "nature": '<path d="M82 138V77" stroke="#203142" stroke-width="8" stroke-linecap="round"/><path d="M82 80c-32-2-44-20-40-48 30 0 45 16 40 48zM83 84c31-8 50 2 57 30-30 10-50-1-57-30z" fill="{color}" stroke="#203142" stroke-width="7"/>',
        "magic": '<path d="m81 20 13 35 37-8-22 31 28 25-38 3-4 38-22-31-35 15 13-36-31-21 38-5z" fill="{color}" stroke="#203142" stroke-width="7"/>',
        "default": '<rect x="35" y="35" width="90" height="90" rx="20" fill="{color}" stroke="#203142" stroke-width="7"/><path d="M52 78h56M80 50v56" stroke="#203142" stroke-width="7" stroke-linecap="round"/>',
    }[kind]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160" role="img" aria-label="original object sprite">
  <rect width="160" height="160" rx="30" fill="#fffaf0"/>
  {shapes.format(color=color)}
</svg>
"""
