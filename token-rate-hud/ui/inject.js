/**
 * token-rate-hud 界面页脚注入脚本（`15:40 · 用时 7分40秒 · 首 token 7秒 · Decode速度 90 tok/s · 有效样本 12/14 · 端到端 45 tok/s · 剔等待2分10秒 52 tok/s · GLM-5.3`）
 *
 * 挂载方式：install 时在渲染层 index.html 加一行 <script defer src="file://…/ui/inject.js">，
 *           由 ZCode 渲染层在启动时加载（幂等闸防重复）。
 * 数据源：http://127.0.0.1:__PORT__/turns（schema 6，本地只读服务，SQLite 用量库）：
 *         Decode速度 = Σ出token ÷ Σ(时长−首输出等待) 的调用均值（宿主时间代理口径），
 *         样本不齐时并排显示「有效样本 N/M」（无效调用两侧同剔，原因悬停可见）；
 *         端到端固定显示整轮墙钟值（出 token ÷ 整轮墙钟）；服务端产出的剔等待值
 *         （tps_e2e_active，仅「有主调用完成且空档无推断成分」的轮产出）另列一段，
 *         标签与数字一体、不被省略号截断；
 *         /live?cid=<本窗口 data-turn-id>（schema 2：live + workflow + subagents 三块，
 *         服务端解析成会话后都限定在本窗口的会话内——多窗口/后台自动化工作流并存时
 *         不串台；cid 未解析时 live_pending=true 且三组数据全空，不回退全局）。
 * 定位锚：ZCode 每轮对话是 <section data-turn-id="…">（虚拟滚动，滚到哪渲染哪；
 *         运行中的轮次另有 data-v4-running-live-tail 尾节点——3.17.1 起它是虚拟
 *         列表 div 且并非全程在 DOM、data-turn-id 轮内会漂移/短暂为空，故 cid 取
 *         「从后往前第一个非空值」、宿主缺位时按轮 id 反查 section 兜底）。
 * 工作流：实时行末尾追加 ⟪ 工作流 活跃/总数 ⟫ 汇总段（~X=并行瞬时合计，
 *         运行均值/调用均值=均值口径，标签明示），其下另起第二行列出逐个工作流子代理与普通 Agent 工具子代理（名字 + ~瞬时 /
 *         均值 / ✓累计，普通子代理附归属方式标注）；轮次结束后静态行按数据版本回填
 *         补「工作流 N 代理 耗Xk token」合计段（速率不并入主代理，避免并行失真）。
 *         主轮已结束但工作流仍在后台跑时，明细行挂到最新一轮区块下继续刷新。
 * 身份核验：/live 请求记录当时的 cid，响应回来先核对窗口 cid 未变、再核对 live.msg_id
 *         属于当前窗口（完整相等或前缀互配），迟到响应一律丢弃不覆盖新状态；
 *         tickLive 单飞（进行中请求不重入）。
 * 设计约束：任何异常静默吞掉，绝不影响主界面；React 若删掉注入节点，observer 会重画。
 */
(() => {
  if (window.__tokenRateFooterLoaded) return;
  window.__tokenRateFooterLoaded = true;

  const API = "http://127.0.0.1:__PORT__";
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

  // 身份比对：完整相等或前缀互配（DOM 属性可能是截断版 id，阈值 8 兼顾短 id 与误配风险）
  function cidMatches(a, b) {
    const x = norm(a);
    const y = norm(b);
    if (!x || !y) return false;
    return (
      x === y ||
      (x.length >= 8 && y.startsWith(x)) ||
      (y.length >= 8 && x.startsWith(y))
    );
  }

  // 带超时的 fetch：挂起连接必须能返回，否则单飞标志被永久占用、实时轮询整段哑掉
  function fetchJSON(url, ms) {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), ms || 4000);
    return fetch(url, { signal: ac.signal })
      .then((r) => r.json())
      .finally(() => clearTimeout(t));
  }

  // 本窗口的身份：取运行中节点（或最后一个 section）的 data-turn-id（本轮 user 消息 id），
  // 报给 /live?cid= 由服务端解析成会话——实时行与工作流块都限定在本窗口的会话内，
  // 多窗口/后台自动化工作流并存时不会串台。
  function domCid() {
    try {
      const tail = document.querySelector(`section[${LIVE_HOST_ATTR}], [data-turn-id][${LIVE_HOST_ATTR}]`);
      const tv = tail && tail.getAttribute("data-turn-id");
      if (tv) return tv;
      // 回退：从后往前取第一个非空 data-turn-id。3.17.1 虚拟列表尾部存在属性值为
      // 空的节点（轮内部分阶段 turnId 未定），盲取最后一个会得到空 cid，服务端只能
      // 回「待解析」，实时行整轮失效（[live-cid] →None 实测）
      const all = document.querySelectorAll("section[data-turn-id], [data-turn-id]");
      for (let i = all.length - 1; i >= 0; i--) {
        const v = all[i].getAttribute("data-turn-id");
        if (v) return v;
      }
      return "";
    } catch {
      return "";
    }
  }

  // 清掉不在目标宿主里的旧实时节点（宿主切换/回收时防残留）：
  //   WF 行只允许挂在当前目标宿主里；
  //   LIVE 行同样以「当前目标宿主」为准（3.17.1 运行尾节点并非全程在 DOM，
  //   宿主可为轮次 section）——轮结束的清理由 tickLive 的无数据分支负责，
  //   被 React 回收/复用的旧节点不再属于当前宿主，一并命中
  function sweepLines(host) {
    try {
      document.querySelectorAll(`[${WF_MARK}]`).forEach((el) => {
        if (!host || !host.contains(el)) el.remove();
      });
      document.querySelectorAll(`[${LIVE_MARK}]`).forEach((el) => {
        if (!host || !host.contains(el)) el.remove();
      });
    } catch {
      /* 静默 */
    }
  }

  async function fetchTurns() {
    if (Date.now() - fetchedAt < CACHE_MS) return;
    try {
      const j = await fetchJSON(`${API}/turns?limit=800&_=${Date.now()}`, 4000);
      if (Array.isArray(j.turns)) {
        turns = j.turns; // 服务端超陈旧上限时 turns 为空列表（宁缺毋假），照实采纳
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

  /**
   * 段结构渲染：每个指标是「标签 + 数字」一体的不可分割单元（inline-block + nowrap），
   * 段间的「 · 」分隔符是唯一可截断点——整行 ellipsis 时只会整段消失，
   * 绝不出现「剔等待」标签被截成半个、数字悬空的样式。
   */
  function renderSegs(line, segs) {
    line.textContent = "";
    segs.forEach((txt, i) => {
      if (i) {
        const sep = document.createElement("span");
        sep.textContent = " · ";
        line.appendChild(sep);
      }
      const s = document.createElement("span");
      s.textContent = txt;
      s.style.display = "inline-block";
      s.style.whiteSpace = "nowrap";
      line.appendChild(s);
    });
  }

  function render(section, t) {
    const posNum = (v) => typeof v === "number" && isFinite(v) && v > 0;
    // 段清单 + 展示签名：签名不含 title，展示字段变了才重写 DOM——
    // 静态行按数据版本回填（晚到用量、账本校正值、工作流尾部归并的 wf_* 都能刷新，
    // 而不是一旦渲染就永不更新）；turns 未变时零 DOM 写。
    const segs = [fmtStamp(t.end_ms), `用时 ${fmtDur(t.run_ms)}`];
    const decTitle = [];
    if (t.ttft_ms != null && t.ttft_ms >= 0) segs.push(`首 token ${fmtLat(t.ttft_ms)}秒`);
    // 双速率并列，各自带标签与单位，不出现无量纲数字：
    //   Decode速度 = Σ出token ÷ Σ(时长−首输出等待) 的调用均值（宿主时间代理口径）
    //   端到端     = 出 token ÷ 整轮墙钟（固定完整墙钟值，绝不静默换分母）
    // 剔空档值（tps_e2e_active）另列：标签「剔等待Xs」与数字一体成段，不被截断。
    // 缺失侧不显示，任何情况下不跨口径冒充（posNum 显式判定 0/缺失/非有限数）
    const vc = t.decode_valid_calls || 0;
    const tc = t.decode_total_calls || 0;
    if (posNum(t.tps)) {
      segs.push(`Decode速度 ${fmtTps(t.tps)} tok/s`);
      // 有效样本覆盖：decode 分子分母同条件剔除无效调用，样本不齐时并排披露
      if (tc > 0 && vc < tc) segs.push(`有效样本 ${vc}/${tc}`);
      decTitle.push(`Decode速度＝首输出后调用均值（Σ出token÷Σ(时长−TTFT)，宿主时间代理口径）`);
      const drops = [];
      if (t.decode_drop_ttft_missing) drops.push(`缺TTFT ${t.decode_drop_ttft_missing}`);
      if (t.decode_drop_ttft_negative) drops.push(`负TTFT ${t.decode_drop_ttft_negative}`);
      if (t.decode_drop_zero_duration) drops.push(`零时长 ${t.decode_drop_zero_duration}`);
      if (t.decode_drop_zero_output) drops.push(`零输出 ${t.decode_drop_zero_output}`);
      if (t.decode_drop_not_completed) drops.push(`未完成 ${t.decode_drop_not_completed}`);
      decTitle.push(`有效样本 ${vc}/${tc}` + (drops.length ? `（剔除：${drops.join("、")}；原因可重叠，一次调用可同时命中多个）` : ""));
    }
    if (posNum(t.tps_e2e)) segs.push(`端到端 ${fmtTps(t.tps_e2e)} tok/s`);
    if (posNum(t.tps_e2e_active)) {
      // 服务端仅对「有主调用完成且空档无推断成分」的轮产出该值（推断轮为 null），
      // 这里不再做秒级门槛判断；等待时长并入段标签，口径切换永远伴随标注
      segs.push(`剔等待${t.wait_ms > 0 ? fmtDur(t.wait_ms) : ""} ${fmtTps(t.tps_e2e_active)} tok/s`);
      decTitle.push(`剔等待值＝整轮输出÷（墙钟−执行前空档合计），仅空档无推断成分的轮产出`);
    }
    if (t.calls > 1) segs.push(`${t.calls} 次调用`);
    if (t.wf_actors) segs.push(`工作流 ${t.wf_actors} 代理 耗${fmtTok(t.wf_total_tokens || t.wf_out_tokens)} tok`);
    if (Array.isArray(t.models) && t.models.length) segs.push(t.models.join("/"));
    if (t.status && t.status !== "completed") segs.push("（已取消）");

    let line = section.querySelector(`[${MARK}]`);
    // 签名含剔除明细：decode 样本覆盖/剔除原因变化（vc/tc 不变时）同样触发回填
    const sig = segs.join("\u0001") + "\u0002" + decTitle.join("\u0001");
    if (!line) {
      line = document.createElement("div");
      line.setAttribute(MARK, "1");
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
    } else if (line.__trSig === sig) {
      return; // 数据版本没变：不重写 DOM
    }
    line.__trSig = sig;
    line.title = decTitle.length ? decTitle.join("；") : "";
    renderSegs(line, segs);
  }

  // ---- 进行中的轮：实时行（每秒拉取 /live，原地刷新；轮结束即移除，交给静态页脚） ----
  let liveDiagOnce = false;
  // 渲染层瞬时速率：后端按消息统计的字符增量（live.instant_tps，服务端逐 assistant
  // 消息采样 text+reasoning 字符，用按模型分桶校准的 chars/token 比换算）优先；
  // 不可用时回退本地 DOM 字符测算（阈值过滤：每秒 10~400 字符视为模型流式——
  // 工具结果一次性倾倒会超上限，React 重排导致的长度回缩直接跳过），仅作估算，显示带 ~ 前缀。
  let liveCalib = 2.4;
  // DOM 回退的采样状态按 (session_id, turn_id, host) 键绑定：切换运行中会话/轮次时
  // 基线重置，绝不把上一轮的字符量差算进本轮。
  let domSample = { key: "", len: 0, ts: 0 };
  // 本地测算只统计目标输出节点：剔除插件自插的三类行与工具调用块——
  // data-tool-call-id 是渲染层工具块的真实标记（含 workflow-tool-summary），
  // 插件行每秒刷新文本，不剔除会把自身抖动算成模型输出。
  const SKIP_SEL = `[${MARK}],[${LIVE_MARK}],[${WF_MARK}],[data-tool-call-id]`;
  function hostTextLen(host) {
    try {
      const clone = host.cloneNode(true);
      clone.querySelectorAll(SKIP_SEL).forEach((el) => el.remove());
      const txt = clone.textContent || "";
      // 注：textContent.length 是 UTF-16 单元数，SQLite length() 按字符计——
      // BMP 外字符（emoji 等）计 2 单元，对「增速÷校准比」的估算影响可忽略
      return txt.length;
    } catch {
      return 0;
    }
  }
  function domInstant(host, live) {
    const key = `${(live && live.session_id) || ""}|${(live && live.turn_id) || ""}|${host.getAttribute("data-turn-id") || ""}`;
    const now = Date.now();
    // 字符串键之外再绑节点引用：同 data-turn-id 的宿主被 React 替换时（属性串相同、
    // 节点已换），旧节点的字数基线对新节点毫无意义，必须重置——否则替换瞬间两节点
    // 的字数差会被当成模型输出（反例：旧节点 100 字、新节点 196 字，间隔 1 秒误产出 ~40 tok/s）
    if (domSample.host !== host || domSample.key !== key) {
      domSample = { host, key, len: hostTextLen(host), ts: now };
      return null; // 基线刚建：本轮首个样本不产出
    }
    const len = hostTextLen(host);
    let tps = null;
    const dt = (now - domSample.ts) / 1000;
    if (dt >= 0.5) {
      const d = len - domSample.len;
      const cps = d / dt;
      if (d >= 10 && cps <= 400 && liveCalib > 0) {
        tps = Math.round((cps / liveCalib) * 10) / 10;
      }
    }
    domSample.len = len;
    domSample.ts = now;
    return tps;
  }

  let liveInFlight = false;
  let lastLiveOkAt = 0; // 最近一次拿到 live 数据的时刻：cid 短暂抖动的拆行宽限基准
  async function tickLive() {
    if (liveInFlight) return; // 单飞：上一拍请求未返回不重入，防迟到响应互相覆盖
    liveInFlight = true;
    try {
      const cid = domCid();
      let j = null;
      try {
        j = await fetchJSON(`${API}/live?cid=${encodeURIComponent(cid)}&_=${Date.now()}`, 4000);
      } catch {
        /* 服务未起/请求超时：静默（超时必返回，单飞标志不会被挂起请求永久占用） */
      }
      // 响应身份核验：请求期间窗口切了轮/换了会话，整包丢弃（迟到响应不覆盖新状态）
      if (j && !cidMatches(domCid(), cid)) return;
      let live = (j && j.live) || null;
      // 身份核验降为会话级：3.17.1 轮内 DOM 的 turn-id 会随消息推进漂移（user 消息
      // id → 各 assistant 消息 id），严格比对 live.msg_id≈cid 会整轮误杀。会话圈定
      // 已由服务端完成（数据按 cid 解析出的 live_scope 查询），这里只拦跨会话串包
      if (live && j && j.live_scope && live.session_id && live.session_id !== j.live_scope) live = null;
      const wf = (j && Array.isArray(j.workflow)) ? j.workflow : [];
      const subs = (j && Array.isArray(j.subagents)) ? j.subagents : [];
      const subAgg = (j && j.sub_agg_instant) || null;
      if (live) lastLiveOkAt = Date.now();
      try {
        let host = document.querySelector(`section[${LIVE_HOST_ATTR}], [data-turn-id][${LIVE_HOST_ATTR}]`);
        // 3.17.1：运行尾节点是虚拟列表 div，并非全程在 DOM。缺位时先按 live 轮的
        // id 反查所属 section，再退到最后一个非空 data-turn-id 的 section
        if (!host) {
          const secs = Array.from(document.querySelectorAll("section[data-turn-id]"));
          if (live && secs.length) {
            for (let i = secs.length - 1; i >= 0; i--) {
              const v = secs[i].getAttribute("data-turn-id") || "";
              if (v && (cidMatches(v, live.msg_id) || (live.turn_id && cidMatches(v, live.turn_id)))) {
                host = secs[i];
                break;
              }
            }
          }
          if (!host && (live || wf.length || subs.length)) {
            for (let i = secs.length - 1; i >= 0; i--) {
              if (secs[i].getAttribute("data-turn-id")) {
                host = secs[i];
                break;
              }
            }
          }
        }
        if (!host && live) {
          // DOM 形态再漂移时的定位线索：节点数、尾节点数、当前轮 id
          diag(`NOHOST nodes=${document.querySelectorAll("[data-turn-id]").length} tail=${document.querySelectorAll(`[${LIVE_HOST_ATTR}]`).length} turn=${(live.turn_id || "").slice(0, 20)}`);
        }
        sweepLines(host);
        let line = host && host.querySelector(`[${LIVE_MARK}]`);
        let wfLine = host && host.querySelector(`[${WF_MARK}]`);
        if (!live && !wf.length && !subs.length) {
          // cid 短暂抖动（空值/未解析）不立即拆行：5 秒宽限内保留已渲染的实时行，
          // 下一拍 cid 恢复即继续刷新；真结束由宽限期后的移除兜底
          const grace = j && j.live_pending && line && Date.now() - lastLiveOkAt < 5000;
          if (!grace) {
            if (line) line.remove();
            if (wfLine) wfLine.remove();
            domSample = { key: "", len: 0, ts: 0 };
          }
          return;
        }
        if (!host) return;
        if (!liveDiagOnce && live) {
          liveDiagOnce = true;
          diag(`LIVE host_id=${(host.getAttribute("data-turn-id") || "?").slice(0, 44)} msg_id=${(live.msg_id || "?").slice(0, 44)}`);
        }
        // 工作流单行汇总段（取最新一个运行）：⟪ 工作流 2/5 ⟫ ~120 tok/s——
        // ~X = 并行瞬时合计（各活跃子代理瞬时值相加），运行均值/调用均值 = 均值口径，
        // 三种不混用：优先瞬时合计；无瞬时值时依次回退 run 均值、调用均值
        let wfSeg = "";
        if (wf.length) {
          const w = wf[0];
          let sp = "";
          if (w.agg_instant) sp = `~${fmtTps(w.agg_instant)} tok/s`;
          else if (w.run_avg_tps) sp = `运行均值 ${fmtTps(w.run_avg_tps)} tok/s`;
          else if (w.tps) sp = `调用均值 ${fmtTps(w.tps)} tok/s`;
          wfSeg = `⟪ 工作流 ${w.n_active}/${w.actors}${wf.length > 1 ? `+${wf.length - 1}` : ""} ⟫${sp ? ` ${sp}` : ""}`;
        }
        // 普通 Agent 工具子代理的并行瞬时合计（sub_agg_instant，口径同 agg_instant）
        let subSeg = "";
        if (subs.length && subAgg) subSeg = `⟪ 子代理 ⟫ ~${fmtTps(subAgg)} tok/s`;
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
          // 瞬时速率：后端按消息统计的增量优先，本地 DOM 测算只作回退
          const inst = (live.instant_tps && live.instant_tps > 0) ? live.instant_tps : domInstant(host, live);
          if (inst) {
            parts.push(`~${fmtTps(inst)} tok/s`);
            if (live.calib) {
              line.title = `流式估算：按消息字符增速÷校准比换算（校准比 ${live.calib}` +
                `，有效样本 ${live.calib_n != null ? live.calib_n : 0}，随调用完成自动校准）`;
            }
          } else {
            if (live.tps) parts.push(`已完成均值 ${fmtTps(live.tps)} tok/s`);
            // 瞬时值作废（超 3 秒无字符增长）或尚无完成调用：清掉流式估算的
            // 悬停说明，title 必须与当前显示口径一致，不残留旧说明
            line.title = "";
          }
          if (live.n_calls > 1) parts.push(`${live.n_calls} 次调用`);
          if (Array.isArray(live.models) && live.models.length) parts.push(live.models.join("/"));
          if (wfSeg) parts.push(wfSeg);
          if (subSeg) parts.push(subSeg);
          line.textContent = parts.join(" · ");
        } else if (line) {
          line.remove(); // 主轮已结束但工作流仍活跃：只留工作流行
        }
        // 第二行：工作流子代理与普通子代理明细（活跃的带 ~ 瞬时速率或「均」调用均值，
        // 完成的打 ✓ 列总消耗）
        if (wf.length || subs.length) {
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
              if (a.active) {
                // 「均」前缀 = 调用均值口径（decode 参考），与 ~ 瞬时合计区分
                return a.tps ? `${shortName(a.name)} 均${fmtTps(a.tps)}` : `${shortName(a.name)} …`;
              }
              // ✓ 后是总消耗（入+出，缓存读在输入侧）：编码类子代理输出仅占 ~1%，
              // 只显示输出会“少得离谱”；显示总消耗才反映真实工作量
              if (a.n_calls) return `${shortName(a.name)} ✓${fmtTok(a.total_tokens != null ? a.total_tokens : a.out_tokens)}`;
              return `${shortName(a.name)} …`;
            });
            segs.push(label + acts.join(" · "));
          }
          if (subs.length) {
            const acts = subs.slice(0, 8).map((b) => {
              // 归属方式：agent_ref（尾注直接归属）是常态不标注；
              // inferred/undetermined 明示——用量照出，归属缺位不冒充
              const how =
                b.attributed_by === "inferred" ? "(推断)" :
                b.attributed_by === "undetermined" ? "(未定)" : "";
              if (b.instant_tps) return `${shortName(b.name)} ~${fmtTps(b.instant_tps)}${how}`;
              if (b.active) {
                return b.tps ? `${shortName(b.name)} 均${fmtTps(b.tps)}${how}` : `${shortName(b.name)} …${how}`;
              }
              if (b.n_calls) return `${shortName(b.name)} ✓${fmtTok(b.total_tokens != null ? b.total_tokens : b.out_tokens)}${how}`;
              return `${shortName(b.name)} …${how}`;
            });
            segs.push(`⟪ 子代理 ⟫ ` + acts.join(" · "));
          }
          const text = segs.join(" ｜ ");
          if (wfLine.__trSig !== text) { // 文本没变不重写 DOM
            wfLine.__trSig = text;
            wfLine.textContent = text;
          }
        } else if (wfLine) {
          wfLine.remove();
        }
      } catch {
        /* 静默 */
      }
    } finally {
      liveInFlight = false;
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
          render(s, t); // 无行则画，已有行按数据版本回填（晚到用量可刷新）
        } else if (s.querySelector(`[${MARK}]`)) {
          matched++; // 数据暂时匹配不到（如服务端 stale 清空）：保留已渲染行不动
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
