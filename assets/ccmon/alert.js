// 三个主题共用的提醒：会话「干完了」「等你了」→ 提示音 + 标签页小图标。
//
// 用法：<script src="/assets/ccmon/alert.js"></script>，每次成功拿到 /api/sessions 后调 CCAlert.observe(data)。
// 提示音开关存 localStorage.ccmonSound（"1"/"0"），三个主题共用；主题里的开关按钮调 CCAlert.setSound(on)。
// 标题（document.title）仍由各主题自己写。
//
// 声音来自 clawd-on-desk：complete.mp3（CC0，Freesound iut_Paris8）、confirm.mp3（随该项目 AGPL-3.0）。
(() => {
  const LS = localStorage;
  const SND = {done: "/assets/sounds/complete.mp3", wait: "/assets/sounds/confirm.mp3"};
  // 忙了不到这么久就停下的，不响「干完了」——你多半正坐在那台机器前一问一答
  const MIN_BUSY_SEC = 20;

  const audio = {};
  let unlocked = false;
  let prev = null;             // sid -> {group, busySince}；null = 还没收到第一轮，第一轮不响
  let lastIcon = "";

  function soundOn() { return LS.ccmonSound === "1"; }

  function play(kind) {
    if (!soundOn()) return;
    try {
      const a = audio[kind] || (audio[kind] = new Audio(SND[kind]));
      a.volume = 0.6;
      a.currentTime = 0;
      a.play().catch(() => {});   // 浏览器要求先有一次点击才能出声，没解锁就静默失败
    } catch {}
  }

  // 第一次点击页面时把两个音频都「摸」一下，之后才能在没有手势的时候出声
  function unlock() {
    if (unlocked) return;
    unlocked = true;
    for (const kind in SND) {
      try {
        const a = audio[kind] || (audio[kind] = new Audio(SND[kind]));
        a.muted = true;
        a.play().then(() => { a.pause(); a.currentTime = 0; a.muted = false; }).catch(() => { a.muted = false; });
      } catch {}
    }
  }
  document.addEventListener("click", unlock, {capture: true});
  document.addEventListener("keydown", unlock, {capture: true});

  function setSound(on) {
    LS.ccmonSound = on ? "1" : "0";
    if (on) { unlock(); setTimeout(() => play("done"), 60); }   // 打开时响一声，让人知道是什么动静
  }

  // ---- 标签页小图标：Clawd 醒着 = 都在干活；Clawd 睡了 = 有会话停下来了；右上红点 = 有会话在等你 ----
  const SRC = {awake: "/assets/clawd/clawd-static-base.svg", asleep: "/assets/clawd/clawd-sleeping.svg"};
  const imgs = {};
  function loadImg(key) {
    return imgs[key] || (imgs[key] = new Promise(res => {
      const i = new Image();
      i.onload = () => res(i);
      i.onerror = () => res(null);
      i.src = SRC[key];
    }));
  }
  async function setIcon(state) {      // "awake" | "asleep" | "wait"
    if (state === lastIcon) return;
    lastIcon = state;
    const key = state === "asleep" ? "asleep" : "awake";
    const img = await loadImg(key);
    if (!img || lastIcon !== state) return;
    const c = document.createElement("canvas");
    c.width = c.height = 64;
    const g = c.getContext("2d");
    g.imageSmoothingEnabled = false;
    if (key === "awake") {
      g.drawImage(img, 2, 0, 60, 64);                       // viewBox 0 0 15 16
    } else {
      // 睡觉那张 viewBox 是 (-15 -25 45 45)，身体趴在 x -1.5..16.5、y 6..17 —— 取一个 18×18 的方框
      const W = img.naturalWidth || 500, u = W / 45;
      g.drawImage(img, (15 - 1.5) * u, (25 + 0.5) * u, 18 * u, 18 * u, 0, 0, 64, 64);
    }
    if (state === "wait") {
      g.fillStyle = "#fff"; g.beginPath(); g.arc(50, 14, 14, 0, 7); g.fill();
      g.fillStyle = "#e5484d"; g.beginPath(); g.arc(50, 14, 11, 0, 7); g.fill();
    }
    let link = document.querySelector('link[rel="icon"]');
    if (!link) { link = document.createElement("link"); link.rel = "icon"; document.head.append(link); }
    link.type = "image/png";
    link.href = c.toDataURL("image/png");
  }

  // data = /api/sessions 的返回。返回这一轮发生的事件，主题想做额外表现（比如弹个气泡）可以用。
  function observe(data) {
    const cur = {};
    const events = [];
    let nWait = 0, nStopped = 0, nBusy = 0;
    for (const grp of (data && data.groups) || []) {
      for (const it of grp.items || []) {
        const g = grp.key;
        if (g === "waiting") nWait++;
        else if (g === "busy") nBusy++;
        else if (g === "idle" || g === "done") nStopped++;
        const p = prev && prev[it.sid];
        const busySince = g === "busy" ? (p && p.group === "busy" ? p.busySince : (it.since || Date.now() / 1000)) : null;
        cur[it.sid] = {group: g, busySince};
        if (!prev || !p || p.group === g) continue;
        if (g === "waiting") events.push({type: "wait", sid: it.sid, item: it});
        else if (p.group === "busy" && (g === "idle" || g === "done")) {
          const ran = Date.now() / 1000 - (p.busySince || 0);
          events.push({type: "done", sid: it.sid, item: it, ranSec: ran, quiet: ran < MIN_BUSY_SEC});
        }
      }
    }
    prev = cur;
    if (events.some(e => e.type === "wait")) play("wait");
    else if (events.some(e => e.type === "done" && !e.quiet)) play("done");
    setIcon(nWait ? "wait" : nStopped ? "asleep" : "awake");
    return events;
  }

  // 登录过期（改了密码 / 换了浏览器）时接口回 401：直接带着当前页面跳去登录，登回来还在原处。
  // 放在这里是因为三个主题都加载这个文件，省得每个主题各写一遍。
  const rawFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const r = await rawFetch(...args);
    if (r.status === 401 && location.pathname !== "/login") {
      location.href = "/login?next=" + encodeURIComponent(location.pathname) + location.hash;
    }
    return r;
  };

  setIcon("awake");
  window.CCAlert = {observe, setSound, soundOn, play, unlock};
})();
