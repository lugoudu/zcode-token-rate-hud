/**
 * token-rate-hud 界面页脚注入脚本（DeepSeek 风格：`15:40 · 用时 7分40秒 · 首 token 7秒 · 49 tok/s · GLM-5.3`）
 *
 * 挂载方式：install 时在渲染层 index.html 加一行 <script defer src="file://…/ui/inject.js">，
 *           由 ZCode 渲染层在启动时加载（幂等闸防重复）。
 * 数据源：http://127.0.0.1:__PORT__/turns（本地只读服务，SQLite 用量库，口径=纯解码速率）、
 *         /live（进行中轮次 + workflow 块：运行中的工作流按子代理聚合）。
 * 定位锚：ZCode 每轮对话是 <section data-turn-id="…">（虚拟滚动，滚到哪渲染哪；
 *         运行中的轮次另有一个 data-v4-running-live-tail 元素，同选择器）。
 * 工作流：实时行末尾追加 ⟪ 工作流 活跃/总数 ⟫ 汇总段，其下另起第二行列出
 *         逐个子代理（名字 + ~瞬时速率 / ✓累计）；轮次结束后静态行补
 *         「工作流 N 代理 +Xk token」合计段（速率不并入主代理，避免并行失真）。
 * 设计约束：任何异常静默吞掉，绝不影响主界面；React 若删掉注入节点，observer 会重画。
 */
(() => {
  if (window.__tokenRateFooterLoaded) return;
  window.__tokenRateFooterLoaded = true;

  const API = "http://127.0.0.1:__PORT__";
  const SHOW_CTX = __SHOW_CTX__;
  const MARK = "data-token-rate-footer";
  const LIVE_MARK = "data-token-rate-live";
  const WF_MARK = "data-token-rate-wf";
  const LIVE_HOST_ATTR = "data-v4-running-live-tail";
  const CACHE_MS = 2000;

  let turns = [];
  let fetchedAt = 0;

  const norm = (s) => String(s || "").replace(/^(turn_|msg_)/, "");

  // 子代理名/运行名截断（第二行明细最长 12 字，超出省略号）
  const shortName = (s) => {
    const t = String(s || "");
    return t.length > 12 ? t.slice(0, 11) + "…" : t;
  };

  async function fetchTurns() {
    if (Date.now() - fetchedAt < CACHE_MS) return;
    try {
      const r = await fetch(`${API}/turns?limit=800&_=${Date.now()}`);
      const j = await r.json();
      if (Array.isArray(j.turns)) {
        turns = j.turns;
        fetchedAt = Date.now();
      }
    } catch {
      /* 服务未起：静默 */
    }
  }

  function fmtDur(ms) {
    const t = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(t / 60);
    const s = t % 60;
    return m > 0 ? `${m}分${String(s).padStart(2, "0")}秒` : `${t}秒`;
  }
  function fmtLat(ms) {
    const s = Math.max(0, (ms || 0) / 1000);
    return s < 10 ? String(+s.toFixed(1)) : String(Math.round(s));
  }
  function fmtTps(v) {
    return v >= 10 ? String(Math.round(v)) : String(+Number(v).toFixed(1));
  }
  function fmtTok(n) {
    return n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(Math.round(n));
  }
  function fmtStamp(ms) {
    const d = new Date(ms);
    const now = new Date();
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    if (d.toDateString() === now.toDateString()) return hm;
    const sameYear = d.getFullYear() === now.getFullYear();
    return sameYear
      ? `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`
      : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
  }

  /**
   * 把统计行挂进与正文相同的排版上下文，避免位置漂移：
   * 追加到最后一个内容块的父容器（继承列宽/居中），并复制其水平内边距，
   * 使左缘与正文对齐；父容器拿不到时回退到 section 本身 + 固定 16px。
   */
  function mountLine(section, line) {
    let host = section;
    try {
      const ref = section.lastElementChild;
      if (ref) {
        const cs = getComputedStyle(ref);
        if (cs && cs.paddingLeft && cs.paddingLeft !== "0px") {
          line.style.paddingLeft = cs.paddingLeft;
          line.style.paddingRight = cs.paddingRight !== "0px" ? cs.paddingRight : cs.paddingLeft;
        }
        if (ref.parentElement && ref !== line) host = ref.parentElement;
      }
    } catch {
      /* 计算样式失败：回退默认 */
    }
    host.appendChild(line);
    return host;
  }

  function render(section, t) {
    if (section.querySelector(`[${MARK}]`)) return;
    const line = document.createElement("div");
    line.setAttribute(MARK, "1");
    const parts = [fmtStamp(t.end_ms), `用时 ${fmtDur(t.run_ms)}`];
    if (t.ttft_ms != null && t.ttft_ms >= 0) parts.push(`首 token ${fmtLat(t.ttft_ms)}秒`);
    if (t.tps) parts.push(`${fmtTps(t.tps)} tok/s`);
    if (SHOW_CTX && t.ctx_tokens) parts.push(`ctx ${fmtTok(t.ctx_tokens)}`);
    if (t.calls > 1) parts.push(`${t.calls} 次调用`);
    if (t.wf_actors) parts.push(`工作流 ${t.wf_actors} 代理 +${fmtTok(t.wf_out_tokens)}`);
    if (Array.isArray(t.models) && t.models.length) parts.push(t.models.join("/"));
    if (t.status && t.status !== "completed") parts.push("（已取消）");
    line.textContent = parts.join(" · ");
    Object.assign(line.style, {
      fontSize: "12px",
      opacity: "0.55",
      padding: "0 16px 4px",
      userSelect: "none",
      whiteSpace: "nowrap",
      overflow: "hidden",
      textOverflow: "ellipsis",
    });
    mountLine(section, line);
  }

  // ---- 进行中的轮：实时行（每秒拉取 /live，原地刷新；轮结束即移除，交给静态页脚） ----
  let liveDiagOnce = false;
  // 渲染层瞬时速率：流式文本在界面上逐字增长，直接测字符增速，
  // 用服务端按真实 token 校准的 chars/token 比（live.calib）换算 tok/s。
  // 阈值过滤：每秒 10~400 字符视为模型流式（工具结果一次性倾倒会超过上限，
  // React 重排导致的长度回缩直接跳过），仅作估算，显示带 ~ 前缀。
  let liveCalib = 2.4;
  let prevLen = 0;
  let prevTs = 0;
  function localInstant(host) {
    let len = 0;
    try {
      len = host.innerText ? host.innerText.length : 0;
    } catch {
      return null;
    }
    const now = Date.now();
    let tps = null;
    const dt = (now - prevTs) / 1000;
    if (prevTs && dt >= 0.5) {
      const d = len - prevLen;
      const cps = d / dt;
      if (d >= 10 && cps <= 400 && liveCalib > 0) {
        tps = Math.round((cps / liveCalib) * 10) / 10;
      }
    }
    prevLen = len;
    prevTs = now;
    return tps;
  }
  async function tickLive() {
    let live = null;
    let wf = [];
    try {
      const r = await fetch(`${API}/live?_=${Date.now()}`);
      const j = await r.json();
      live = j.live || null;
      wf = Array.isArray(j.workflow) ? j.workflow : [];
    } catch {
      /* 服务未起：静默 */
    }
    try {
      const host = document.querySelector(`section[${LIVE_HOST_ATTR}], [data-turn-id][${LIVE_HOST_ATTR}]`);
      let line = host && host.querySelector(`[${LIVE_MARK}]`);
      let wfLine = host && host.querySelector(`[${WF_MARK}]`);
      if (!live && !wf.length) {
        if (line) line.remove();
        if (wfLine) wfLine.remove();
        prevLen = 0;
        prevTs = 0;
        return;
      }
      if (!host) return;
      if (!liveDiagOnce && live) {
        liveDiagOnce = true;
        diag(`LIVE host_id=${(host.getAttribute("data-turn-id") || "?").slice(0, 44)} msg_id=${(live.msg_id || "?").slice(0, 44)}`);
      }
      // 工作流单行汇总段（取最新一个运行）：⟪ 工作流 2/5 ⟫ ~120 tok/s
      let wfSeg = "";
      if (wf.length) {
        const w = wf[0];
        const sp = w.agg_instant
          ? `~${fmtTps(w.agg_instant)} tok/s`
          : w.tps
            ? `${fmtTps(w.tps)} tok/s`
            : "";
        wfSeg = `⟪ 工作流 ${w.n_active}/${w.actors}${wf.length > 1 ? `+${wf.length - 1}` : ""} ⟫${sp ? ` ${sp}` : ""}`;
      }
      if (live) {
        if (!line) {
          line = document.createElement("div");
          line.setAttribute(LIVE_MARK, "1");
          Object.assign(line.style, {
            fontSize: "12px",
            opacity: "0.75",
            padding: "0 16px 4px",
            userSelect: "none",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          });
          mountLine(host, line);
        }
        const parts = [fmtStamp(live.start_ms), `进行中 ${fmtDur(live.elapsed_ms)}`];
        if (!live.n_calls) parts.push("首步生成中…");
        if (live.ttft_ms != null && live.ttft_ms >= 0) parts.push(`首 token ${fmtLat(live.ttft_ms)}秒`);
        if (live.calib) liveCalib = live.calib;
        const inst = localInstant(host) || live.instant_tps;
        if (inst) parts.push(`~${fmtTps(inst)} tok/s`);
        else if (live.tps) parts.push(`${fmtTps(live.tps)} tok/s`);
        if (SHOW_CTX && live.ctx_tokens) parts.push(`ctx ${fmtTok(live.ctx_tokens)}`);
        if (live.n_calls > 1) parts.push(`${live.n_calls} 次调用`);
        if (Array.isArray(live.models) && live.models.length) parts.push(live.models.join("/"));
        if (wfSeg) parts.push(wfSeg);
        line.textContent = parts.join(" · ");
      } else if (line) {
        line.remove(); // 主轮已结束但工作流仍活跃：只留工作流行
      }
      // 第二行：逐个子代理明细（活跃的带 ~ 瞬时速率，完成的打 ✓）
      if (wf.length) {
        if (!wfLine) {
          wfLine = document.createElement("div");
          wfLine.setAttribute(WF_MARK, "1");
          Object.assign(wfLine.style, {
            fontSize: "12px",
            opacity: "0.6",
            padding: "0 16px 4px",
            userSelect: "none",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          });
          mountLine(host, wfLine);
        }
        const segs = [];
        for (const w of wf) {
          const label = wf.length > 1 && w.run_name ? `${shortName(w.run_name)}：` : "";
          const acts = (w.per_actor || []).slice(0, 8).map((a) => {
            if (a.instant_tps) return `${shortName(a.name)} ~${fmtTps(a.instant_tps)}`;
            if (a.active) return a.tps ? `${shortName(a.name)} ${fmtTps(a.tps)}` : `${shortName(a.name)} …`;
            if (a.n_calls) return `${shortName(a.name)} ✓${fmtTok(a.out_tokens)}`;
            return `${shortName(a.name)} …`;
          });
          segs.push(label + acts.join(" · "));
        }
        wfLine.textContent = segs.join(" ｜ ");
      } else if (wfLine) {
        wfLine.remove();
      }
    } catch {
      /* 静默 */
    }
  }

  // 诊断上报：仅在首次扫描与匹配失败时发一条，便于定位界面结构变化
  let lastReport = "";
  function diag(msg) {
    if (msg === lastReport) return;
    lastReport = msg;
    try {
      fetch(`${API}/diag?msg=${encodeURIComponent(msg.slice(0, 1200))}`).catch(() => {});
    } catch {}
  }

  let pending = false;
  let scannedOnce = false;
  async function scan() {
    if (pending) return;
    pending = true;
    try {
      const secs = document.querySelectorAll("section[data-turn-id], [data-turn-id][data-v4-running-live-tail]");
      if (!secs.length) return;
      if (!scannedOnce) {
        scannedOnce = true;
        diag(
          "IDS " +
            [...secs]
              .slice(0, 6)
              .map((s) => (s.getAttribute("data-turn-id") || "?").slice(0, 44))
              .join(",")
        );
      }
      await fetchTurns();
      let matched = 0;
      secs.forEach((s) => {
        if (s.hasAttribute(LIVE_HOST_ATTR)) return; // 运行中的节点由实时行负责
        if (s.querySelector(`[${MARK}]`)) {
          matched++;
          return;
        }
        const domId = norm(s.getAttribute("data-turn-id"));
        let t = turns.find((x) => norm(x.msg_id) === domId || norm(x.turn_id) === domId);
        if (!t && domId.length >= 8) {
          // 防御：DOM 属性若是截断版，退化为前缀匹配（阈值 8 兼顾短 id 与误配风险）
          t = turns.find((x) => {
            const m = norm(x.msg_id);
            return m.startsWith(domId) || domId.startsWith(m);
          });
        }
        if (t) {
          matched++;
          render(s, t);
        }
      });
      if (matched === 0) {
        diag(`NOMATCH dom=${secs.length} cache=${turns.length} first=${(secs[0].getAttribute("data-turn-id") || "?").slice(0, 44)}`);
      }
    } catch {
      /* 静默 */
    } finally {
      pending = false;
    }
  }

  function start() {
    try {
      const mo = new MutationObserver(() => {
        clearTimeout(mo._t);
        mo._t = setTimeout(scan, 300);
      });
      mo.observe(document.body, { childList: true, subtree: true });
      setInterval(scan, 4000);
      setInterval(tickLive, 1000);
      tickLive();
      scan();
    } catch {
      /* 静默 */
    }
  }

  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);
})();
