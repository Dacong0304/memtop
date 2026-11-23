/* MemTop Web — 前端逻辑（无依赖，原生 DOM） */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const TOKEN = $('meta[name="memtop-token"]').content;
  const VERSION = $('meta[name="memtop-version"]').content;
  const GAUGE_C = 2 * Math.PI * 80;

  const state = {
    snap: null,
    groups: [],
    expanded: new Set(),
    selected: null,
    query: "",
    onlyMine: false,
    sort: "mem",
    interval: 2000,
    timer: null,
    limit: 150,
    loading: true,
    paused: false,
  };

  let pending = null; // 待确认的结束计划

  // ------------------------------------------------------------ 工具

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  function fmtSize(bytes) {
    const n = Number(bytes) || 0;
    if (n >= 1024 ** 4) return (n / 1024 ** 4).toFixed(2) + " TB";
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(n / 1024 ** 3 >= 100 ? 0 : 1) + " GB";
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(n / 1024 ** 2 >= 100 ? 0 : 0) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
    return n + " B";
  }

  const fmtPct = (part, total) => (total > 0 ? (part / total) * 100 : 0);

  function relTime(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 1) return "刚刚";
    if (sec < 60) return sec + " 秒前";
    return Math.floor(sec / 60) + " 分钟前";
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || ("HTTP " + res.status));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function post(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-MemTop-Token": TOKEN },
      body: JSON.stringify(body),
    });
  }

  // ------------------------------------------------------------ Toast

  function toast(message, kind = "info", action = null) {
    const box = $("#toasts");
    const el = document.createElement("div");
    el.className = "toast" + (kind === "warn" ? " warn" : kind === "err" ? " err" : "");
    el.innerHTML = '<i></i><div class="t-body"><p>' + esc(message) + "</p>" +
      (action && action.note ? "<small>" + esc(action.note) + "</small>" : "") +
      "</div>";
    if (action) {
      const btn = document.createElement("button");
      btn.textContent = action.label;
      btn.addEventListener("click", () => { el.remove(); action.run(); });
      el.appendChild(btn);
    }
    box.appendChild(el);
    const ttl = action ? 12000 : kind === "err" ? 8000 : 5000;
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 220);
    }, ttl);
    while (box.children.length > 4) box.firstElementChild.remove();
  }

  // ------------------------------------------------------------ 概览

  function renderSummary(mem, meta) {
    const pctUsed = fmtPct(mem.used, mem.total);
    $("#usedPct").textContent = pctUsed.toFixed(0);
    $("#usedText").textContent = fmtSize(mem.used) + " / " + fmtSize(mem.total);

    const gauge = $("#gauge");
    gauge.dataset.level = pctUsed >= 88 ? "hot" : pctUsed >= 72 ? "warn" : "ok";
    const stops = pctUsed >= 88
      ? ["#ffbcbc", "#ff7b7b", "#ff5f5f"]
      : pctUsed >= 72
        ? ["#ffd79a", "#f2b45c", "#ff9d6f"]
        : ["#5eead4", "#57d6c8", "#6aa8ff"];
    $$("#gaugeGrad stop").forEach((s, i) => s.setAttribute("stop-color", stops[i] || stops[0]));
    $("#gaugeFill").style.strokeDashoffset = String(GAUGE_C * (1 - Math.min(pctUsed, 100) / 100));

    const parts = [
      ["app", "应用", mem.app],
      ["wired", "有线", mem.wired],
      ["compressed", "压缩", mem.compressed],
      ["cached", "缓存", mem.cached],
      ["free", "空闲", mem.free],
    ];
    const stack = $("#stack");
    stack.innerHTML = parts.map(([cls, , v]) =>
      '<div class="seg ' + cls + '" style="width:' + Math.max(0, Math.min(100, fmtPct(v, mem.total))).toFixed(2) + '%"></div>'
    ).join("");
    // 误差补齐：让分段正好铺满
    const sum = parts.reduce((a, [, , v]) => a + v, 0);
    if (sum < mem.total && stack.lastElementChild) {
      stack.lastElementChild.style.width =
        (Math.max(0, Math.min(100, fmtPct(mem.free + (mem.total - sum), mem.total)))).toFixed(2) + "%";
    }

    $("#legend").innerHTML = parts.map(([cls, label, v]) => {
      const sortable = cls === "compressed" ? ' data-sort="compressed" title="按压缩占用排序"' :
        cls === "app" ? ' data-sort="mem" title="按内存占用排序"' : "";
      const active = (state.sort === "compressed" && cls === "compressed") ||
        (state.sort === "mem" && cls === "app") ? " active" : "";
      return '<li class="' + active + '"' + sortable + '><i class="' + cls + '"></i><span>' +
        label + "</span><b>" + fmtSize(v) + "</b></li>";
    }).join("");

    $("#swapText").textContent = mem.swap_total > 0
      ? fmtSize(mem.swap_used) + " / " + fmtSize(mem.swap_total)
      : "未使用";
    $("#swapText").title = "交换空间";

    const pressure = $("#pressureChip");
    pressure.dataset.level = String(mem.pressure);
    pressure.querySelector("em").textContent = "压力 " + (mem.pressure_label || "—");

    $("#sampleMeta").textContent = meta ? "采样 " + (meta.sample_ms / 1000).toFixed(2) + " s" : "";
  }

  function renderMachine(meta) {
    if (!meta || !meta.machine) return;
    const m = meta.machine;
    const total = state.snap ? fmtSize(state.snap.memory.total) : "";
    $("#machineChip").textContent = [m.host, "macOS " + (m.macos || ""), total]
      .filter(Boolean).join(" · ");
    $("#machineChip").title = [m.chip, m.arch].filter(Boolean).join(" · ");
  }

  // ------------------------------------------------------------ 列表

  function visibleGroups() {
    const q = state.query.trim().toLowerCase();
    let rows = state.groups.filter((g) => {
      if (state.onlyMine && !g.mine) return false;
      if (!q) return true;
      if (g.name.toLowerCase().includes(q)) return true;
      if (String(g.main_pid) === q) return true;
      return g.members.some((m) =>
        m.name.toLowerCase().includes(q) || String(m.pid) === q ||
        (m.path || "").toLowerCase().includes(q));
    });
    const by = state.sort;
    rows.sort((a, b) => {
      if (by === "name") return a.name.localeCompare(b.name, "zh-Hans-CN") || b.footprint - a.footprint;
      if (by === "compressed") return b.compressed - a.compressed || b.footprint - a.footprint;
      return b.footprint - a.footprint;
    });
    return rows;
  }

  function iconHTML(g) {
    const letter = esc((g.name || "?").trim().charAt(0).toUpperCase());
    if (g.bundle) {
      return '<img class="icon" alt="" loading="lazy" data-letter="' + letter +
        '" src="/api/icon?bundle=' + encodeURIComponent(g.bundle) + '">';
    }
    return '<span class="icon letter">' + letter + "</span>";
  }

  function memberRows(g) {
    const head =
      '<div class="member head"><div>进程 / PID</div><div class="m-num">内存</div>' +
      '<div class="m-num">压缩</div><div class="m-num">运行</div>' +
      '<div class="m-num">用户</div><div class="m-act"></div></div>';
    const list = g.members.map((m) => {
      const flag = m.protected
        ? '<span class="m-tag">' + esc(m.reason || "受保护") + "</span>"
        : m.risk === "caution"
          ? '<span class="m-tag warn">主进程</span>'
          : "";
      const kill = m.protected
        ? '<button disabled title="' + esc(m.reason || "受保护") + '">受保护</button>'
        : '<button data-act="kill-member" data-pid="' + m.pid + '">结束</button>';
      return '<div class="member">' +
        '<div class="m-name">' + (flag ? "" : '<span class="dash">·</span>') +
        "<span>" + esc(m.name) + '</span><em>' + m.pid + "</em>" + flag + "</div>" +
        '<div class="m-num">' + fmtSize(m.footprint) + "</div>" +
        '<div class="m-num">' + (m.compressed ? fmtSize(m.compressed) : '<span class="dash">—</span>') + "</div>" +
        '<div class="m-num">' + esc(m.elapsed || "—") + "</div>" +
        '<div class="m-num">' + esc(m.user) + "</div>" +
        '<div class="m-act">' + kill + "</div></div>";
    }).join("");
    return head + list;
  }

  function groupRow(g, maxMem) {
    const open = state.expanded.has(g.key);
    const killable = g.members.filter((m) => !m.protected);
    const mine = g.mine;
    const user = (g.members[0] || {}).user || "—";
    const pct = fmtPct(g.footprint, state.snap.memory.total);
    const caret = g.members.length > 1
      ? '<button class="caret" data-act="toggle" aria-label="展开" aria-expanded="' + open + '">' +
        '<svg viewBox="0 0 24 24" width="13" height="13"><path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>'
      : '<button class="caret blank" tabindex="-1"></button>';
    const tags = [];
    if (killable.length === 0) tags.push('<span class="badge lock">受保护</span>');
    else if (g.risk === "caution") tags.push('<span class="badge warn" title="结束可能丢失未保存内容">慎</span>');

    const killBtn = killable.length
      ? '<button class="kill-btn" data-act="kill-group" title="结束 ' + killable.length + ' 个可结束进程">结束</button>'
      : '<button class="kill-btn" disabled>受保护</button>';

    const row =
      '<tr class="row' + (open ? " open" : "") + (state.selected === g.key ? " sel" : "") +
      '" data-key="' + esc(g.key) + '">' +
      '<td><div class="proc-cell">' + caret + iconHTML(g) +
      '<div class="proc-name"><span class="name">' + esc(g.name) + "</span>" +
      '<span class="meta">' + g.members.length + " 个进程 · " + esc(user) +
      (mine ? "" : " · 其他用户") + "</span></div>" + tags.join("") + "</div></td>" +
      '<td><div class="mem-cell"><b class="mem-val">' + fmtSize(g.footprint) + "</b>" +
      '<span class="mem-bar' + (pct >= 12 ? " hot" : "") + '"><i style="width:' +
      Math.max(2, Math.min(100, (g.footprint / maxMem) * 100)).toFixed(1) + '%"></i></span></div></td>' +
      '<td class="pct-cell">' + pct.toFixed(1) + "%</td>" +
      '<td class="comp-cell">' + (g.compressed ? "<b>" + fmtSize(g.compressed) + "</b>" : '<span class="dash">—</span>') + "</td>" +
      '<td class="user-cell">' + esc(user) + "</td>" +
      '<td style="text-align:right">' + killBtn + "</td></tr>";

    if (!open) return row;
    return row + '<tr class="row members"><td colspan="6"><div class="member-list">' +
      memberRows(g) + "</div></td></tr>";
  }

  function renderList() {
    const rows = visibleGroups();
    const shown = rows.slice(0, state.limit);
    const maxMem = Math.max(1, ...state.groups.map((g) => g.footprint));
    $("#rows").innerHTML = shown.map((g) => groupRow(g, maxMem)).join("");
    $("#skeleton").hidden = true;
    $("#empty").hidden = rows.length !== 0;

    const total = state.groups.length;
    $("#groupCount").textContent = rows.length === total
      ? total + " 组"
      : rows.length + " / " + total + " 组";
    $("#footNote").innerHTML = state.snap
      ? "共 <b>" + state.snap.processes + "</b> 个进程 · " + total + " 组" +
        (state.onlyMine ? " · 仅显示我的" : "") +
        (rows.length > shown.length
          ? ' · <button class="more" data-act="more">显示全部 ' + rows.length + " 组</button>"
          : "")
      : "—";

    // 图标加载失败 → 首字母方块
    $$("#rows img.icon").forEach((img) => {
      img.addEventListener("error", () => {
        const span = document.createElement("span");
        span.className = "icon letter";
        span.textContent = img.dataset.letter || "?";
        img.replaceWith(span);
      }, { once: true });
    });
  }

  // ------------------------------------------------------------ 结束流程

  function openKill(targets, opts) {
    targets = targets.filter((t) => !t.protected);
    if (!targets.length) {
      toast("没有可结束的进程（受保护）", "warn");
      return;
    }
    const needsAck = targets.some((t) => t.risk === "caution");
    pending = {
      targets,
      groupKey: (opts && opts.groupKey) || null,
      protectName: (opts && opts.protectName) || null,
      needsAck,
    };

    $("#killTitle").textContent = "结束 " + ((opts && opts.label) || targets[0].name);
    $("#killSub").textContent = targets.length + " 个进程 · 先 SIGTERM，3 秒未退出可再强杀";

    const shown = targets.slice(0, 10);
    $("#killTargets").innerHTML = shown.map((t) =>
      '<div class="target"><span class="t-name">' + esc(t.name) + "</span>" +
      '<span class="t-pid">' + t.pid + "</span>" +
      '<span class="t-size">' + fmtSize(t.footprint) + "</span>" +
      (t.risk === "caution" ? '<span class="t-flag">主进程</span>' : "") + "</div>"
    ).join("") + (targets.length > shown.length
      ? '<div class="target"><span class="t-name dash">… 还有 ' + (targets.length - shown.length) + " 个</span></div>"
      : "");

    const total = targets.reduce((a, t) => a + t.footprint, 0);
    $("#killReclaim").textContent = "≈ " + fmtSize(total);

    $("#killWarn").hidden = !needsAck;
    $("#ackInput").value = "";
    $("#ackInput").placeholder = "yes";
    $("#killConfirm").disabled = needsAck;
    $("#killConfirm").textContent = "结束进程";
    $("#killProtect").hidden = !(opts && opts.protectName);
    $("#killProtect").textContent = "改为保护 " + ((opts && opts.protectName) || "");

    $("#killOverlay").hidden = false;
    setTimeout(() => {
      if (needsAck) $("#ackInput").focus();
      else $("#killConfirm").focus();
    }, 30);
  }

  function closeKill() {
    $("#killOverlay").hidden = true;
    pending = null;
  }

  async function doKill() {
    if (!pending) return;
    if (pending.needsAck && $("#ackInput").value.trim().toLowerCase() !== "yes") return;
    const btn = $("#killConfirm");
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "结束中…";
    const targets = pending.targets;
    try {
      const data = await post("/api/kill", {
        pids: targets.map((t) => t.pid),
        signal: "TERM",
        grace: 3,
        force: false,
        ack: true,
      });
      const r = data.result || {};
      const survived = (r.survivors || []).length;
      const failed = (r.failed || []).length;
      closeKill();
      let msg = "已结束 " + (r.sent || []).length + " 个进程 · 预计回收 " + fmtSize(r.reclaimed_est || 0);
      if (failed) msg += " · " + failed + " 个失败";
      toast(msg, survived ? "warn" : "info", survived ? {
        label: "强制结束",
        note: survived + " 个进程 3 秒内没有退出",
        run: () => forceKill(r.survivors.map((s) => s.pid)),
      } : null);
      await load();
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "结束进程";
      if (e.status === 409 && e.data && e.data.need_ack) {
        $("#killWarn").hidden = false;
        $("#ackInput").focus();
        toast("需要先确认：包含应用主进程", "warn");
      } else {
        toast("结束失败：" + e.message, "err");
      }
    }
  }

  async function forceKill(pids) {
    try {
      const data = await post("/api/kill", { pids, signal: "KILL", grace: 1, force: true, ack: true });
      const r = data.result || {};
      const left = (r.survivors || []).length;
      toast(left ? "仍有 " + left + " 个进程未退出（可能无权限）" : "已强制结束 " + (r.sent || []).length + " 个进程",
        left ? "err" : "info");
      await load();
    } catch (e) {
      toast("强杀失败：" + e.message, "err");
    }
  }

  async function addProtect(pattern) {
    if (!pattern) return;
    try {
      const data = await post("/api/protect", { action: "add", pattern });
      toast("已加入保护：" + pattern);
      renderProtect(data.patterns);
      await load();
    } catch (e) {
      toast("加入失败：" + e.message, "err");
    }
  }

  // ------------------------------------------------------------ 安全中心

  function renderProtect(patterns) {
    const box = $("#protectChips");
    if (!patterns || !patterns.length) {
      box.innerHTML = '<span class="none">还没有自定义保护项</span>';
      return;
    }
    box.innerHTML = patterns.map((p) =>
      '<span class="chip-item">' + esc(p) +
      '<button data-act="protect-rm" data-pattern="' + esc(p) + '" title="移除">×</button></span>'
    ).join("");
  }

  async function loadProtect() {
    try {
      const data = await api("/api/protect");
      renderProtect(data.patterns);
      $("#protectPath").textContent = data.file || "";
    } catch (e) {
      toast("读取保护名单失败：" + e.message, "err");
    }
  }

  async function loadLog() {
    try {
      const data = await api("/api/log?limit=40");
      const box = $("#logList");
      if (!data.rows.length) {
        box.innerHTML = '<li class="none">还没有结束记录</li>';
      } else {
        box.innerHTML = data.rows.map((r) => {
          const t = (r.ts || "").replace("T", " ").slice(5);
          const names = (r.names || []).join("、");
          const surv = (r.survivors || []).length
            ? '<div class="surv">未退出：' + r.survivors.join(", ") + "</div>" : "";
          return '<li><div class="log-top"><time>' + esc(t) + "</time>" +
            '<span class="sig">' + esc(r.sig || "SIGTERM") + "</span></div>" +
            '<div class="names">' + esc(names || "—") + "</div>" + surv + "</li>";
        }).join("");
      }
      $("#logPath").textContent = data.file || "";
    } catch (e) {
      toast("读取日志失败：" + e.message, "err");
    }
  }

  function openSafety(tab) {
    $("#safetyOverlay").hidden = false;
    switchTab(tab || "protect");
    loadProtect();
    loadLog();
  }

  function switchTab(tab) {
    $$("#safetyTabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    $$(".tab-panel").forEach((p) => { p.hidden = p.dataset.panel !== tab; });
    if (tab === "log") loadLog();
  }

  // ------------------------------------------------------------ 数据

  async function load() {
    try {
      const data = await api("/api/snapshot");
      state.snap = data;
      state.groups = data.groups || [];
      state.loading = false;
      $("#liveDot").dataset.state = state.paused ? "paused" : "ok";
      renderMachine(data.meta);
      renderSummary(data.memory, data.meta);

      const reclaim = data.memory.reclaimable || 0;
      $("#reclaimText").textContent = fmtSize(reclaim);
      $("#compressRelease").textContent = fmtSize(data.memory.compressed_killable || 0);
      $("#compressRelease").title = "结束这些进程可回收的压缩页；压缩本身由内核管理，无法直接释放";
      $("#reclaimText").title = "当前用户可结束的进程合计（含共享内存，偏大）";
      $("#procText").textContent = data.processes + " 个";

      renderList();
      tickFreshness();
    } catch (e) {
      state.loading = false;
      $("#skeleton").hidden = true;
      $("#liveDot").dataset.state = "error";
      $("#freshness").textContent = "读取失败，重试中…";
      if (!state.warned) {
        state.warned = true;
        toast("无法读取快照：" + e.message, "err");
      }
      $("#empty").hidden = state.groups.length > 0;
    }
  }

  function tickFreshness() {
    if (!state.snap) return;
    const label = $("#freshness");
    if (state.paused) {
      label.textContent = "已暂停 · " + relTime(state.snap.age);
      return;
    }
    const age = state.snap.age + (Date.now() - state.fetchedAt) / 1000;
    label.textContent = "更新于 " + relTime(age) + " · 每 " + (state.interval / 1000) + " 秒";
  }

  function schedule() {
    clearTimeout(state.timer);
    if (state.paused) return;
    state.timer = setTimeout(async () => {
      if (!document.hidden) await load();
      state.fetchedAt = Date.now();
      schedule();
    }, state.interval);
  }

  // ------------------------------------------------------------ 事件

  function bind() {
    $("#search").addEventListener("input", (e) => {
      state.query = e.target.value;
      state.limit = 150;
      renderList();
    });

    $("#onlyMine").addEventListener("change", (e) => {
      state.onlyMine = e.target.checked;
      state.limit = 150;
      renderList();
    });

    $("#interval").addEventListener("change", (e) => {
      const v = Number(e.target.value);
      state.paused = v === 0;
      state.interval = v || 2000;
      $("#liveDot").dataset.state = state.paused ? "paused" : "ok";
      state.fetchedAt = Date.now();
      tickFreshness();
      schedule();
      if (!state.paused) load();
    });

    $("#legend").addEventListener("click", (e) => {
      const li = e.target.closest("li[data-sort]");
      if (!li) return;
      state.sort = li.dataset.sort;
      state.limit = 150;
      $$("#sortSeg button").forEach((x) => x.classList.toggle("on", x.dataset.sort === state.sort));
      renderList();
      if (state.sort === "compressed" && !state.compressedExplained) {
        state.compressedExplained = true;
        toast("压缩内存由内核管理，用户态无法直接释放；按压缩排序后结束进程，才能回收对应的压缩页。", "info");
      }
    });

    $$("#sortSeg button").forEach((b) => b.addEventListener("click", () => {
      state.sort = b.dataset.sort;
      state.limit = 150;
      $$("#sortSeg button").forEach((x) => x.classList.toggle("on", x === b));
      renderList();
    }));

    $("#rows").addEventListener("click", (e) => {
      const actEl = e.target.closest("[data-act]");
      const row = e.target.closest("tr[data-key]");
      const group = row ? state.groups.find((g) => g.key === row.dataset.key) : null;

      if (actEl && actEl.dataset.act === "more") {
        state.limit = 100000;
        renderList();
        return;
      }
      if (actEl && actEl.dataset.act === "toggle" && group) {
        toggleGroup(group.key);
        return;
      }
      if (actEl && actEl.dataset.act === "kill-group" && group) {
        state.selected = group.key;
        openKill(group.members, {
          label: group.name,
          groupKey: group.key,
          protectName: group.name,
        });
        return;
      }
      if (actEl && actEl.dataset.act === "kill-member" && group) {
        const m = group.members.find((x) => String(x.pid) === actEl.dataset.pid);
        if (m) openKill([m], { label: m.name, groupKey: group.key });
        return;
      }
      if (row && group) {
        state.selected = group.key;
        $$("#rows tr.row").forEach((tr) => tr.classList.toggle("sel", tr === row));
        if (!actEl) toggleGroup(group.key);
      }
    });

    $("#killCancel").addEventListener("click", closeKill);
    $("#killConfirm").addEventListener("click", doKill);
    $("#killOverlay").addEventListener("click", (e) => {
      if (e.target.id === "killOverlay") closeKill();
    });
    $("#ackInput").addEventListener("input", (e) => {
      $("#killConfirm").disabled = e.target.value.trim().toLowerCase() !== "yes";
    });
    $("#ackInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !$("#killConfirm").disabled) doKill();
    });
    $("#killProtect").addEventListener("click", async () => {
      if (!pending || !pending.protectName) return;
      const name = pending.protectName;
      closeKill();
      await addProtect(name);
    });

    $("#btnSafety").addEventListener("click", () => openSafety("protect"));
    $("#safetyClose").addEventListener("click", () => { $("#safetyOverlay").hidden = true; });
    $("#safetyOverlay").addEventListener("click", (e) => {
      if (e.target.id === "safetyOverlay") $("#safetyOverlay").hidden = true;
    });
    $$("#safetyTabs button").forEach((b) =>
      b.addEventListener("click", () => switchTab(b.dataset.tab)));

    $("#protectForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("#protectInput");
      const v = input.value.trim();
      if (!v) return;
      input.value = "";
      await addProtect(v);
    });

    $("#protectChips").addEventListener("click", async (e) => {
      const btn = e.target.closest('[data-act="protect-rm"]');
      if (!btn) return;
      try {
        const data = await post("/api/protect", { action: "rm", pattern: btn.dataset.pattern });
        toast("已移除保护：" + btn.dataset.pattern);
        renderProtect(data.patterns);
        await load();
      } catch (err) {
        toast("移除失败：" + err.message, "err");
      }
    });

    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
      if (e.key === "Escape") {
        if (!$("#killOverlay").hidden) return closeKill();
        if (!$("#safetyOverlay").hidden) return void ($("#safetyOverlay").hidden = true);
        if (typing) return void document.activeElement.blur();
        if (state.query) { state.query = ""; $("#search").value = ""; renderList(); }
        return;
      }
      if (typing) return;
      if (e.key === "/") { e.preventDefault(); $("#search").focus(); return; }
      if (e.key === "r") { e.preventDefault(); load(); return; }
      if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); return; }
      if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); return; }
      if (e.key === "Enter") {
        if (state.selected) { e.preventDefault(); toggleGroup(state.selected); }
        return;
      }
      if (e.key === "x") {
        const g = state.groups.find((x) => x.key === state.selected);
        if (!g) return toast("先选一个应用（j / k 移动）", "warn");
        openKill(g.members, { label: g.name, groupKey: g.key, protectName: g.name });
      }
    });
  }

  function toggleGroup(key) {
    if (!state.expanded.has(key)) state.expanded.add(key);
    else state.expanded.delete(key);
    state.selected = key;
    renderList();
  }

  function moveSelection(dir) {
    const rows = visibleGroups().slice(0, state.limit);
    if (!rows.length) return;
    let i = rows.findIndex((g) => g.key === state.selected);
    i = i < 0 ? (dir > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, i + dir));
    state.selected = rows[i].key;
    renderList();
    const tr = $('#rows tr[data-key="' + CSS.escape(state.selected) + '"]');
    if (tr) tr.scrollIntoView({ block: "nearest" });
  }

  // ------------------------------------------------------------ 启动

  async function boot() {
    bind();
    const sortParam = new URLSearchParams(location.search).get("sort");
    if (["mem", "compressed", "name"].includes(sortParam)) {
      state.sort = sortParam;
      $$("#sortSeg button").forEach((x) => x.classList.toggle("on", x.dataset.sort === sortParam));
    }
    state.fetchedAt = Date.now();
    await load();
    schedule();
    setInterval(tickFreshness, 1000);
    console.info("MemTop Web " + VERSION);

    // ?demo=expand|kill|safety —— 仅用于截图/演示，正常访问不受影响
    const demo = new URLSearchParams(location.search).get("demo");
    if (demo && state.groups.length) {
      const g = state.groups[0];
      if (demo === "expand") { toggleGroup(g.key); }
      if (demo === "kill") {
        openKill(g.members, { label: g.name, groupKey: g.key, protectName: g.name });
      }
      if (demo === "safety") { openSafety("protect"); }
    }
  }

  boot();
})();
