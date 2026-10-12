// Clawd —— Claude Code 的像素小螃蟹。三个主题（猫舍 / 农场 / 夜景公寓）共用这一个文件。
//
// 素材：~/ccmon/assets/clawd/*.svg，拷自 rullerzhou-afk/clawd-on-desk（AGPL-3.0，只在本机内网自用，别外发）。
// 每个 svg 自带 CSS 动画，用 <img> 引用就会自己动；换 src 会让动画从头播，所以 set() 只在文件真变了时才换。
//
// 坐标：47 个 svg 共用 viewBox (-15 -25 45 45)，单位 = 1 个 Clawd 像素。身体占 x 0..15、y 6..16（脚底 y=16）。
// 道具会伸出身体：打字的显示器到 y≈-7，搭积木到 x≈23，抛球/思考的气泡到 y≈-2。
// FRAME 是统一取景框，所有状态都放得下，切换状态时 Clawd 不会跳位置。k = 每个 Clawd 像素占几个屏幕像素，取整数才锐利。
(() => {
  const BASE = "/assets/clawd/";
  const VB = {x: -15, y: -25, w: 45, h: 45};
  const FRAME = {x: -8.5, y: -9, w: 32, h: 26};      // 身体水平居中（x 中心 7.5），脚底在框内 y=25
  const BODY = {x: 0, y: 6, w: 15, h: 10};            // 只框身体（小图标、头像用）

  const F = name => `clawd-${name}.svg`;
  // 「在干活」的几种样子。按会话稳定分配，不同会话一眼可分。
  const WORK = ["working-typing", "working-building", "working-carrying", "working-sweeping",
                "working-thinking", "working-debugger", "working-wizard"].map(F);

  function hash(s) {
    let h = 2166136261;
    for (const c of String(s)) h = Math.imul(h ^ c.codePointAt(0), 16777619);
    return h >>> 0;
  }

  // 干活姿势的分配：每个会话第一次出现时，领一个「眼下在场的会话里用得最少」的姿势，之后不变。
  // 纯哈希会撞（实测两个会话都分到搭积木，看上去一模一样）。存 localStorage，刷新、换主题都不换装。
  const CAST_KEY = "ccmonClawdCast";
  let cast = {}, castSaved = 0;
  try { cast = JSON.parse(localStorage[CAST_KEY] || "{}") || {}; } catch { cast = {}; }
  function castIndex(key, nowSec) {
    let c = cast[key];
    if (!c) {
      const used = new Array(WORK.length).fill(0);
      for (const k in cast) if (nowSec - cast[k].t < 90) used[cast[k].i % WORK.length]++;
      const start = hash(key) % WORK.length;      // 并列时从哈希位置起找，避免总是先发打字
      let best = start;
      for (let d = 0; d < WORK.length; d++) {
        const i = (start + d) % WORK.length;
        if (used[i] < used[best]) best = i;
      }
      c = cast[key] = {i: best, t: nowSec};
      castSaved = 0;
    }
    c.t = nowSec;
    if (nowSec - castSaved > 30) {
      for (const k in cast) if (nowSec - cast[k].t > 5 * 86400) delete cast[k];
      try { localStorage[CAST_KEY] = JSON.stringify(cast); } catch {}
      castSaved = nowSec;
    }
    return c.i;
  }

  // 子代理最近 8 秒有动静就算在跑（与 /api/sessions 契约一致）
  function activeSubagents(s, nowSec) {
    return (s.subagents || []).filter(a => nowSec - (a.mtime || 0) < 8).length;
  }

  // 会话 → svg 文件名。group 是 /api/sessions 里的分组键；since 是当前状态开始的 epoch 秒。
  // 照搬 clawd-on-desk 自己的状态机：子代理多 → 抛球，一个 → 戴耳机摇摆；闲下来先开心、再打哈欠、打盹、最后睡死。
  function pick(s, nowSec = Date.now() / 1000, opts = {}) {
    const g = s.group || s.state;
    const idleFor = s.since ? nowSec - s.since : 0;
    switch (g) {
      case "waiting": return F("notification");
      case "busy": {
        const n = activeSubagents(s, nowSec);
        if (n >= 2) return F("working-juggling");
        if (n === 1) return F("headphones-groove");
        const pool = opts.work || WORK;
        return pool[castIndex(s.sid || s.name, nowSec) % pool.length];
      }
      case "done": return idleFor < 6 ? F("happy") : F("idle-yawn");
      case "idle":
        if (idleFor < 60) return F("idle-yawn");
        if (idleFor < 600) return F("idle-doze");
        return F("sleeping");
      case "dead": return F("idle-collapse");
      case "history": return F("sleeping");
      default: return F("idle-follow");
    }
  }

  // 生成一个取景好的 Clawd 节点：<span class="clawd"><img></span>
  //   k     每个 Clawd 像素多少屏幕像素（建议整数：总览 4–6，列表 2–3，小图标 1–2）
  //   frame 取景框，默认 FRAME；只要身体用 Clawd.BODY
  function el(file, k = 4, frame = FRAME) {
    const box = document.createElement("span");
    box.className = "clawd";
    box.style.cssText = `display:inline-block;position:relative;overflow:hidden;flex:none;` +
                        `width:${frame.w * k}px;height:${frame.h * k}px;`;
    const img = document.createElement("img");
    img.alt = "";
    img.draggable = false;
    img.style.cssText = `position:absolute;max-width:none;image-rendering:pixelated;` +
                        `width:${VB.w * k}px;height:${VB.h * k}px;` +
                        `left:${(VB.x - frame.x) * k}px;top:${(VB.y - frame.y) * k}px;`;
    img.src = BASE + file;
    box.append(img);
    box.dataset.file = file;
    return box;
  }

  // 换状态。文件没变就什么都不做（否则动画会每次轮询都重播）。
  function set(box, file) {
    if (!box || box.dataset.file === file) return false;
    box.dataset.file = file;
    box.querySelector("img").src = BASE + file;
    return true;
  }

  // 预加载，避免状态切换时闪一下空白
  function preload(files = [...WORK, ...["notification", "happy", "idle-yawn", "idle-doze", "sleeping",
      "idle-collapse", "working-juggling", "headphones-groove", "mini-crabwalk", "mini-sleep"].map(F)]) {
    for (const f of files) { const i = new Image(); i.src = BASE + f; }
  }

  window.Clawd = {pick, el, set, preload, hash, activeSubagents, F, WORK, FRAME, BODY, VB, BASE};
})();
