#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tokrate.py —— ZCode token 速率核心库与 CLI（token-rate-hud 插件）

数据源（纯本地，不上传任何数据）：
  ~/.zcode/cli/rollout/model-io-sess_<sessionId>.jsonl
  ZCode 引擎在每次模型调用完成后追加一条记录，含 startedAt / completedAt /
  durationMs / response.usage.{inputTokens, outputTokens, cacheReadTokens…} /
  model.{modelId, role} / turnId。debug 模式下同款文件位于
  ~/.zcode/cli/debug/（自动兼容）。

用法：
  tokrate.py hook reset   供 SessionStart hook 内部复用：轻量重置节流状态
  tokrate.py session_start 供 SessionStart hook 调用：重置 + 按需拉起界面数据服务
                          （hook 全路径零上下文注入，绝不输出 additionalContext）
  tokrate.py report [--session SID | auto] [--turns N] [--calls N]
  tokrate.py serve  [--port 7864] [--session SID | auto]
  tokrate.py footer [--limit N]    打印界面页脚将要展示的轮数据（调试）
  tokrate.py ui-install [--port N]
  tokrate.py ui-status
  tokrate.py ui-uninstall
  tokrate.py live                  # 打印进行中轮 + 工作流/子代理块（调试）
  tokrate.py line   [--session SID | auto]   # 只打印将要注入的实时行（调试）

界面页脚（可选 UI 模块）：把统计行画在每条回答下方（DeepSeek 风格）。ZCode 渲染层
没有插件 UI 接口，该模块通过给 app.asar 注入一行脚本实现：ui-install 自动备份原包、
注入、重打包、校验；ui-uninstall 还原；ZCode 升级覆盖后重跑 ui-install 即可。

环境变量配置：
  TOKEN_RATE_KEEP      状态中保留的最近调用条数（默认 128；轮完整性由按轮
                       独立累计量保证，见 TURN_AGG_KEEP）
  TOKEN_RATE_IDLE_EXIT 服务空闲多少秒后自动退出（默认 21600；launchd 常驻进程设为
                       999999999 即不退出，由 launchd KeepAlive 保活）

（TOKEN_RATE_INTERVAL 已下线：HUD 行上下文注入按产品「零注入」承诺整体移除，
 PostToolUse/Stop hook 路径不复存在。）

口径说明：部分通道（GLM 编程套餐）的 inputTokens 已包含 cacheReadTokens，
部分通道（Anthropic 风格）则不包含。本工具按 “inputTokens >= cacheReadTokens
则视为包含” 自适应——这是数值启发式，无法区分“总输入 100 含缓存 80”与
“非缓存 100 另有缓存 80”两种协议语义，后者会低估输入。会话累计直接使用
归一后的输入总数（缓存读作为其中细分单列，不再叠加双计）。入速率使用
去重后的有效入 token。
"""

import fcntl
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usage_db  # noqa: E402

HOME = os.path.expanduser("~")
ROLLOUT_DIR = os.path.join(HOME, ".zcode", "cli", "rollout")
DEBUG_DIR = os.path.join(HOME, ".zcode", "cli", "debug")
STATE_DIR = os.path.join(HOME, ".zcode", "token-rate-hud")
UI_DIR = os.path.join(STATE_DIR, "ui")
STATE_TTL = 14 * 86400  # 状态文件保留天数

KEEP_CALLS = int(os.environ.get("TOKEN_RATE_KEEP") or 128)
ROLLING_WINDOW = 90.0  # 滚动窗口秒数
TURN_AGG_KEEP = 6      # 状态里按轮独立累计的最近轮数（防长轮被调用条数窗口截断）
NO_REPLY_TAG = "（token 遥测，无需回应）"

# 实时数据服务的陈旧上限：连续查询失败超过该秒数后不再下发旧值（宁缺毋假）
LIVE_MAX_STALE_S = 30.0
# 调试入口 scope 哨兵：/live?global=1 时用它走全局查询（唯一的全局路径）
GLOBAL_SCOPE = "__global__"
# /live 响应字段口径图例（渲染层据此区分瞬时合计与各类均值，防混用）
LIVE_LEGEND = {
    "agg_instant": "并行瞬时合计（各活跃子代理流式瞬时值相加）",
    "instant_tps": "单代理流式瞬时估算（字符增量 ÷ 校准比）",
    "tps": "调用均值（decode 参考：Σout÷Σ(duration−ttft)）",
    "decode_ref_tps": "调用均值（= tps 同值并列，明确口径）",
    "call_avg_tps": "调用均速（Σout÷Σduration，含首包等待）",
    "task_avg_tps": "任务均值（Σout÷批次/actor 墙钟）",
    "run_avg_tps": "run 均值（去重调用 Σout÷run 墙钟）",
    "sub_agg_instant": "并行瞬时合计（普通子代理，同 agg_instant 口径）",
}

MARK_OK = "⚡token-rate"


# ---------------------------------------------------------------- 基础工具

def parse_ts(iso):
    """ISO8601(UTC, 含 Z) -> epoch 秒；失败返回 None。"""
    if not iso or not isinstance(iso, str):
        return None
    try:
        s = iso.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def fmt_tok(n):
    """1234 -> '1.2k'；1234567 -> '1.2M'。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    if n < 1000:
        return str(int(n))
    if n < 1000 * 1000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1000 / 1000:.2f}M"


def fmt_rate(n):
    """速率：>=100 取整，否则保留 1 位小数。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    if n >= 100:
        return str(int(round(n)))
    return f"{n:.1f}"


def fmt_dur(sec):
    sec = int(round(sec or 0))
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def fmt_clock(epoch):
    try:
        return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "-"


# ---------------------------------------------------------------- 会话文件

def rollout_path(session_id):
    if not session_id:
        return None
    name = session_id if session_id.startswith("sess_") else f"sess_{session_id}"
    for d in (ROLLOUT_DIR, DEBUG_DIR):
        p = os.path.join(d, f"model-io-{name}.jsonl")
        if os.path.exists(p):
            return p
    return None


def newest_rollout():
    """mtime 最新的 model-io 文件（当前活跃会话），返回 (sid, path)。"""
    best = (0, None, None)
    for d in (ROLLOUT_DIR, DEBUG_DIR):
        for p in glob.glob(os.path.join(d, "model-io-sess_*.jsonl")):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > best[0]:
                sid = os.path.basename(p)[len("model-io-"):-len(".jsonl")]
                best = (m, sid, p)
    return best[1], best[2]


def resolve_session(session_id):
    """返回 (sid, path)；session_id 为空或 'auto' 时取最新活跃会话。"""
    if session_id and session_id != "auto":
        p = rollout_path(session_id)
        if p:
            return session_id, p
        return None, None
    sid, p = newest_rollout()
    return sid, p


# ---------------------------------------------------------------- 增量解析

def parse_call(obj):
    """从一条 model_io 记录提取精简调用信息；无 usage 的（如失败重试）返回 None。"""
    resp = obj.get("response")
    if not isinstance(resp, dict):
        return None
    u = resp.get("usage")
    if not isinstance(u, dict):
        return None
    t0 = parse_ts(obj.get("startedAt"))
    t1 = parse_ts(obj.get("completedAt"))
    dur = obj.get("durationMs")
    try:
        dur = float(dur) / 1000.0 if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    if t1 is None:
        return None
    if dur is None:
        dur = max((t1 - t0) if t0 else 0.0, 0.0)
    m = obj.get("model") or {}
    out = u.get("outputTokens") or 0
    in_raw = u.get("inputTokens") or 0
    cr = u.get("cacheReadTokens") or 0
    # 口径自适应：inputTokens >= cacheReadTokens 视为“已包含缓存读”（GLM 通道），
    # 否则按 Anthropic 风格相加。in_eff = 有效入 token（去重缓存）。
    in_eff = in_raw if in_raw >= cr else in_raw + cr
    in_nc = (in_raw - cr) if in_raw >= cr else in_raw  # 非缓存入 token
    return {
        "t0": t0 or t1, "t1": t1, "dur": dur,
        "out": out,
        "in": in_raw,
        "cr": cr,
        "in_eff": in_eff,
        "in_nc": in_nc,
        "cw": u.get("cacheWriteTokens") or 0,
        "model": m.get("modelId") or "?",
        "role": m.get("role") or "main",
        "turn": obj.get("turnId") or "",
        "rid": obj.get("requestId") or "",
    }


def state_paths(sid):
    base = os.path.join(STATE_DIR, f"state-{(sid or 'none').replace('/', '_')}")
    return base + ".json", base + ".lock"


def prune_states(keep_sid):
    try:
        now = time.time()
        for p in glob.glob(os.path.join(STATE_DIR, "state-*.json")):
            if keep_sid and os.path.basename(p).startswith(f"state-{keep_sid}"):
                continue
            if now - os.path.getmtime(p) > STATE_TTL:
                os.remove(p)
    except OSError:
        pass


def _turn_agg_from_calls(calls):
    """从调用列表重建按轮累计器（旧状态文件迁移 / 截断重建时起步用）。

    注意：窗口内重建只能近似（早于窗口的调用已不在），轮完整性靠持续
    追加维护。"""
    agg = {}
    for c in calls:
        a = agg.setdefault(c["turn"], {"n": 0, "out": 0, "in": 0, "cr": 0,
                                       "cw": 0, "dur": 0.0,
                                       "t0": c["t0"], "t1": c["t1"]})
        a["n"] += 1
        a["out"] += c["out"]
        a["in"] += c.get("in_eff", c["in"] + c["cr"])
        a["cr"] += c["cr"]
        a["cw"] += c["cw"]
        a["dur"] += c["dur"]
        a["t0"] = min(a["t0"], c["t0"])
        a["t1"] = max(a["t1"], c["t1"])
    return agg


def load_calls(session_id, path, keep=KEEP_CALLS):
    """
    增量读取 rollout 文件。状态文件缓存已消费的字节偏移、累计总量、按轮独立
    累计量与最近 N 条调用。返回 (calls, totals, state)。

    并发安全：读状态 → 解析追加 → 写回的整个读改写在同一把文件锁内完成
    （原先读/解析在锁外，两个进程交错会把 last_inject 写回旧 state 或重复计数）。
    轮完整性：turn_agg 按轮维护独立累计量，长轮（>128 次调用）统计不再被
    calls 窗口截断（滚动窗口等仍走窗口）。

    尾行保护：JSONL 正被宿主追加时最后半行可能写了一半——offset 只推进到
    最后一个「以换行结尾的完整行」边界，不完整尾行起点保留到下次重读，
    绝不因半行解析失败永久漏记该调用。
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    sp, lp = state_paths(session_id)
    empty_totals = {"n": 0, "out": 0, "in": 0, "cr": 0, "cw": 0, "dur": 0.0}

    with open(lp, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            state = {}
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if not isinstance(state, dict) or state.get("v") != 2:
                    state = {}
            except (OSError, ValueError):
                state = {}

            calls = state.get("calls") or []
            totals = dict(state.get("totals") or empty_totals)
            agg = state.get("turn_agg") if isinstance(state.get("turn_agg"), dict) else None
            offset = int(state.get("offset") or 0)

            try:
                size = os.path.getsize(path)
            except OSError:
                size = offset  # 文件暂不可见：视作无新增，保住已有状态

            if offset > size:  # 文件被截断/轮转，从头重建
                offset, calls, totals, agg = 0, [], dict(empty_totals), {}

            if agg is None:
                agg = _turn_agg_from_calls(calls)  # 旧状态迁移：窗口内近似起步

            if offset < size:
                appended_calls = []
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    complete_end = offset
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        if not line.endswith("\n"):
                            break  # 尾行未写完（追加中）：保留起点，下次完整重读
                        complete_end = f.tell()
                        line = line.strip()
                        if not line or '"usage"' not in line:
                            continue  # 快速跳过无 usage 的失败/心跳行（可能携带超大请求体）
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue  # 完整行但内容损坏：跳过不回退（重读也无用）
                        c = parse_call(obj)
                        if c:
                            appended_calls.append(c)
                    offset = complete_end
                for c in appended_calls:
                    totals["n"] += 1
                    totals["out"] += c["out"]
                    totals["in"] += c["in_eff"]
                    totals["cr"] += c["cr"]
                    totals["cw"] += c["cw"]
                    totals["dur"] += c["dur"]
                    a = agg.setdefault(c["turn"], {"n": 0, "out": 0, "in": 0,
                                                   "cr": 0, "cw": 0, "dur": 0.0,
                                                   "t0": c["t0"], "t1": c["t1"]})
                    a["n"] += 1
                    a["out"] += c["out"]
                    a["in"] += c["in_eff"]
                    a["cr"] += c["cr"]
                    a["cw"] += c["cw"]
                    a["dur"] += c["dur"]
                    a["t0"] = min(a["t0"], c["t0"])
                    a["t1"] = max(a["t1"], c["t1"])
                if appended_calls:
                    calls = (calls + appended_calls)[-keep:]
                    if len(agg) > TURN_AGG_KEEP:  # 只留最近活动轮的完整累计
                        for tid in sorted(agg, key=lambda t: agg[t]["t1"])[:-TURN_AGG_KEEP]:
                            agg.pop(tid, None)
                state["v"] = 2
                state["offset"] = offset
                state["calls"] = calls
                state["totals"] = totals
                state["turn_agg"] = agg
                tmp = sp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False)
                os.replace(tmp, sp)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    return calls, totals, state


# ---------------------------------------------------------------- 统计计算

def stats_for(calls, totals, session_id, turn_agg=None):
    """汇总当前调用、本轮、滚动窗口、会话累计与上下文占用。

    turn_agg 传 load_calls 返回 state["turn_agg"] 时，本轮段用按轮独立累计量
    （长轮不被 calls 条数窗口截断）；缺失时回退窗口近似（turn_full=False）。"""
    if not calls:
        return None
    last = calls[-1]
    cur_turn = last["turn"]

    agg = (turn_agg or {}).get(cur_turn)
    if agg:
        turn_n = agg["n"]
        turn_out = agg["out"]
        turn_in = agg["in"]
        turn_span = max(agg["t1"] - agg["t0"], 0.0)
        turn_dur_sum = agg["dur"]
        turn_full = True
    else:
        turn_calls = [c for c in calls if c["turn"] == cur_turn]
        turn_n = len(turn_calls)
        turn_out = sum(c["out"] for c in turn_calls)
        turn_in = sum(c.get("in_eff", c["in"] + c["cr"]) for c in turn_calls)
        turn_span = max(
            (max(c["t1"] for c in turn_calls) - min(c["t0"] for c in turn_calls)) if turn_calls else 0.0, 0.0
        )
        turn_dur_sum = sum(c["dur"] for c in turn_calls)
        turn_full = False
    turn_calls_win = [c for c in calls if c["turn"] == cur_turn]
    rates = [c["out"] / c["dur"] for c in turn_calls_win if c["dur"] > 0.2 and c["out"] > 0]

    now = last["t1"]
    win = [c for c in calls if now - c["t1"] <= ROLLING_WINDOW]
    win_out = sum(c["out"] for c in win)
    win_dur = sum(c["dur"] for c in win) or 1e-9

    total_gen = totals["dur"] or 1e-9

    return {
        "session": session_id,
        "model": last["model"],
        "current": {
            "dur": last["dur"], "out": last["out"], "in": last["in"], "cr": last["cr"],
            "rate_out": (last["out"] / last["dur"]) if last["dur"] > 0 else 0.0,
            "rate_in": (last.get("in_nc", last["in"]) / last["dur"]) if last["dur"] > 0 else 0.0,
        },
        "turn": {
            "id": cur_turn, "n": turn_n, "span": turn_span,
            "out": turn_out, "in": turn_in, "full": turn_full,
            "avg": (turn_out / turn_dur_sum) if turn_dur_sum > 0 else 0.0,
            "peak": max(rates) if rates else 0.0,
        },
        "rolling": {"rate": win_out / win_dur, "out": win_out, "n": len(win)},
        "totals": {
            "n": totals["n"], "out": totals["out"],
            # totals["in"] 已是口径归一后的输入总数（GLM 通道含缓存读），再加 cr 会双计
            "in": totals["in"], "cr": totals["cr"],
            "gen": totals["dur"],
            "avg": totals["out"] / total_gen,
        },
    }


# ---------------------------------------------------------------- 注入文案

def line_post(s):
    cur, turn = s["current"], s["turn"]
    return (
        f"{MARK_OK}｜出 {fmt_rate(cur['rate_out'])} tok/s · "
        f"入 {fmt_rate(cur['rate_in'])} tok/s · 缓存读 {fmt_tok(cur['cr'])}｜"
        f"本轮 {turn['n']} 次 · 出 {fmt_tok(turn['out'])}（均 {fmt_rate(turn['avg'])}）"
        f"{NO_REPLY_TAG}"
    )


# ---------------------------------------------------------------- CLI 模式

def read_hook_stdin():
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        obj = json.loads(raw) if raw.strip() else {}
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def mode_hook(sub):
    """hook 兼容入口：仅保留 reset（SessionStart 的轻量重置）。

    产品承诺零上下文注入：旧 post/stop 的 additionalContext 输出路径已整体
    移除，SessionStart 由 mode_session_start 显式分发、绝不落入输出路径；
    其余未知参数直接静默返回。"""
    if sub != "reset":
        return

    # 轻量重置：只清理过期状态文件。旧「清空节流状态」的重写已移除——
    # PostToolUse 注入下线后 last_inject 再无写入方（残留键无人读），而
    # 无锁读 + 截断写状态文件会与 load_calls 的锁内读改写互相覆盖
    # （或让读取方见到半份 JSON）；如需恢复状态重写必须与 load_calls 共锁
    obj = read_hook_stdin()
    sid = obj.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or \
        os.environ.get("ZCODE_SESSION_ID") or ""
    prune_states(sid)


def mode_session_start():
    """SessionStart hook：重置节流状态；若界面页脚启用，按需拉起本地数据服务。

    注意：本模式由 hook 调用，stdout 必须保持为空（绝不输出 additionalContext），
    所有提示写入 ui/server.log 日志文件。"""
    mode_hook("reset")
    if os.path.exists(ENABLED_FLAG):
        msg = _ensure_server(_server_port(), quiet=True)
        ui_log(f"[session_start] {msg}")


def mode_live():
    """调试：打印当前进行中轮次的实时行（全局口径，显式调试入口）。

    与 /live?global=1 服务端调试路径同源：本函数是 CLI 下的全局查询唯一
    入口之一，HTTP 服务默认按窗口 cid 限定会话、不回退全局。"""
    try:
        import usage_db as _u
        lv = _u.live_turn()
    except Exception as exc:
        print(f"（查询失败：{exc}）")
        return 1
    if not lv:
        print("（当前无进行中的轮）")
        return 0
    parts = [fmt_clock(lv["start_ms"] / 1000), f"进行中 {fmt_dur(lv['elapsed_ms'] / 1000)}"]
    if lv["ttft_ms"] is not None and lv["ttft_ms"] >= 0:
        parts.append(f"首 token {lv['ttft_ms'] / 1000:.1f}s")
    if lv["tps"]:
        parts.append(f"{fmt_rate(lv['tps'])} tok/s")
    parts.append(f"{lv['n_calls']} 次调用")
    if lv["models"]:
        parts.append("/".join(lv["models"]))
    print(" · ".join(parts))
    print(f"  桥 msg_id={lv['msg_id']} turn={lv['turn_id']} session={lv['session_id']}")
    try:
        wf = _u.workflow_live() or []
    except Exception:
        wf = []
    for run in wf:
        state = "运行中" if run["status"] == "running" else run["status"]
        agg = f"~{fmt_rate(run['agg_instant'])} " if run.get("agg_instant") else ""
        cum = f"{fmt_rate(run['tps'])}" if run.get("tps") else "--"
        print(f"\n⟪ 工作流 {run['run_name'] or run['run_id']} · {state} · "
              f"{run['n_active']}/{run['actors']} 代理 · {agg}{cum} tok/s · "
              f"{fmt_tok(run['out_tokens'])} tok ⟫")
        for a in run["per_actor"][:12]:
            seg = (f"~{fmt_rate(a['instant_tps'])}" if a.get("instant_tps")
                   else (fmt_rate(a["tps"]) if a.get("tps") else "--"))
            mark = "●" if a["active"] else "·"
            print(f"  {mark} {a['name']:<16} {seg:>9} tok/s   "
                  f"{a['n_calls']:>3} 次 出{fmt_tok(a['out_tokens'])} 耗{fmt_tok(a.get('total_tokens') or 0)}")
    # 普通 Agent 工具子代理（批次聚合，口径与工作流一致）
    try:
        subs = _u.subagent_live() or []
    except Exception:
        subs = []
    for b in subs[:12]:
        seg = fmt_rate(b["tps"]) if b.get("tps") else "--"
        mark = "●" if b["active"] else "·"
        how = {"agent_ref": "尾注", "inferred": "推断", "undetermined": "?"}.get(
            b.get("attributed_by"), b.get("attributed_by"))
        print(f"⟪ 子代理 {mark} {b['name']:<16} {seg:>9} tok/s · {how} · "
              f"{b['n_calls']:>3} 次 出{fmt_tok(b['out_tokens'])} 耗{fmt_tok(b.get('total_tokens') or 0)} ⟫")
    return 0


def mode_line(session_id):
    sid, path = resolve_session(session_id)
    if not path:
        print("（未找到 rollout 数据源）")
        return 1
    calls, totals, state = load_calls(sid, path)
    s = stats_for(calls, totals, sid, state.get("turn_agg"))
    print(line_post(s) if s else "（暂无 usage 数据）")
    return 0


def mode_report(session_id, n_turns, n_calls):
    sid, path = resolve_session(session_id)
    if not path:
        print("未找到 model-io 数据源（需要 ZCode ≥ rollout 日志版本）")
        return 1
    calls, totals, state = load_calls(sid, path)
    if not calls:
        print(f"会话 {sid} 暂无 usage 记录")
        return 0
    turn_agg = state.get("turn_agg") or {}
    s = stats_for(calls, totals, sid, turn_agg)

    print(f"⚡ token-rate 会话报告  {sid}")
    print(f"模型 {s['model']} · 共 {totals['n']} 次模型调用")
    print("（本报告速率均为含首包等待（TTFT）的调用均值；与页脚 Decode 列的"
          "「首输出后」口径不同，勿混用）\n")

    print(f"最近调用（新→旧，至多 {n_calls} 条）")
    print(f"  {'时间':<9}{'时长':>7}{'出tok':>8}{'出速率(含首包)':>14}{'入tok':>9}{'缓存读':>9}")
    for c in reversed(calls[-n_calls:]):
        rate = (c["out"] / c["dur"]) if c["dur"] > 0 else 0.0
        print(f"  {fmt_clock(c['t1']):<9}{fmt_dur(c['dur']):>7}{c['out']:>8}"
              f"{fmt_rate(rate) + '/s':>14}{c['in']:>9}{fmt_tok(c['cr']):>9}")

    # 按轮汇总优先用按轮独立累计量（完整轮，防长轮被 128 条窗口截断）；
    # 只在窗口内出现、累计器已裁剪的旧轮以窗口近似展示并标注截断
    turns = []
    agg_seen = set()
    for tid, a in turn_agg.items():
        turns.append((a["t1"], tid, a["n"], a["out"], a["in"], a["dur"],
                      a["t1"] - a["t0"], True))
        agg_seen.add(tid)
    seen = {}
    for c in calls:
        if c["turn"] in agg_seen:
            continue
        seen.setdefault(c["turn"], []).append(c)
    for tid, cs in seen.items():
        turns.append((max(x["t1"] for x in cs), tid, len(cs),
                      sum(x["out"] for x in cs),
                      sum(x.get("in_eff", x["in"] + x["cr"]) for x in cs),
                      sum(x["dur"] for x in cs),
                      max(x["t1"] for x in cs) - min(x["t0"] for x in cs), False))
    turns.sort(reverse=True)
    print(f"\n按轮汇总（至多 {n_turns} 轮；状态列=数据完整性）")
    print(f"  {'轮次':<20}{'调用':>5}{'墙钟':>9}{'出tok':>9}{'均出速率(含首包)':>15}{'入tok':>10}  状态")
    for _, tid, n, out, tin, dur_sum, span, full in turns[:n_turns]:
        dur_sum = dur_sum or 1e-9
        print(f"  {tid[:18]:<20}{n:>5}{fmt_dur(max(span, 0.0)):>9}{fmt_tok(out):>9}"
              f"{fmt_rate(out / dur_sum) + '/s':>15}{fmt_tok(tin):>10}"
              f"  {'完整' if full else '窗口近似(截断)'}")

    t = s["totals"]
    print(f"\n会话累计：出 {fmt_tok(t['out'])} · 入 {fmt_tok(t['in'])}（缓存读 {fmt_tok(t['cr'])}）· "
          f"累计模型调用 {fmt_dur(t['gen'])} · 调用均值出速率（含首包）{fmt_rate(t['avg'])} tok/s")
    return 0


# ---------------------------------------------------------------- 实时仪表盘

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>⚡ token-rate 实时仪表盘</title>
<style>
 body{background:#101418;color:#d7dde3;font:14px/1.6 -apple-system,"PingFang SC",sans-serif;
      margin:0;padding:28px;font-variant-numeric:tabular-nums}
 .big{font-size:64px;font-weight:700;line-height:1}
 .unit{font-size:20px;color:#8a97a3;margin-left:8px}
 .row{display:flex;gap:28px;flex-wrap:wrap;margin:18px 0 6px}
 .card{background:#171d24;border:1px solid #232c36;border-radius:10px;padding:14px 18px;min-width:170px}
 .k{color:#8a97a3;font-size:12px}.v{font-size:22px;font-weight:600;margin-top:4px}
 canvas{background:#171d24;border:1px solid #232c36;border-radius:10px;margin-top:14px}
 .meta{color:#8a97a3;font-size:12px;margin-top:10px}
 .wfrow{display:flex;gap:12px;padding:3px 0;font-size:13px;align-items:baseline}
 .wfrow .nm{min-width:10em}
 .dim{color:#8a97a3}
</style></head><body>
<div class="big"><span id="rate">--</span><span class="unit">tok/s 出（最近调用均值）</span></div>
<div class="row">
 <div class="card"><div class="k">入速率（当前调用）</div><div class="v" id="rin">--</div></div>
 <div class="card"><div class="k">本轮均值 / 峰值</div><div class="v" id="turn">--</div></div>
 <div class="card"><div class="k">会话累计出 / 入</div><div class="v" id="tot">--</div></div>
</div>
<canvas id="spark" width="880" height="140"></canvas>
<div id="wfwrap" style="display:none">
 <div class="k" style="margin:16px 0 6px">⟪ 工作流子代理 ⟫</div>
 <div id="wf"></div>
</div>
<div class="meta" id="meta">连接中…</div>
<script>
const f=n=>n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n|0);
const r=n=>n>=100?String(Math.round(n)):n.toFixed(1);
const esc=s=>String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function renderWf(ws){
 if(!ws||!ws.length){wfwrap.style.display='none';return}
 wfwrap.style.display='';
 wf.innerHTML=ws.map(w=>{
  const st=w.status==='running'?'运行中':(w.status==='completed'?'已完成':w.status);
  let h='<div style="margin:10px 0 4px;font-weight:600">'+esc(w.run_name||w.run_id)
   +' <span class="dim">· '+st+' · '+w.n_active+'/'+w.actors+' 代理'
   +(w.agg_instant?' · ~'+r(w.agg_instant)+' tok/s':'')
   +(w.tps?' · 累计 '+r(w.tps)+' tok/s':'')
   +' · 出 '+f(w.out_tokens)+' / 耗 '+f(w.total_tokens||0)+' tok</span></div>';
  h+=(w.per_actor||[]).map(a=>'<div class="wfrow"><span style="width:1em">'
   +(a.active?'●':'·')+'</span><span class="nm">'+esc(a.name)+'</span><span>'
   +(a.instant_tps?('~'+r(a.instant_tps)+' tok/s')
      :(a.active?(a.tps?(r(a.tps)+' tok/s'):'生成中…')
        :(a.n_calls?('✓ 耗 '+f(a.total_tokens!=null?a.total_tokens:a.out_tokens)+' tok'):'等待中')))
   +'</span><span class="dim">'+a.n_calls+' 次 · 出 '+f(a.out_tokens)
   +(a.model?' · '+esc(a.model):'')+'</span></div>').join('');
  return h;
 }).join('');
}
async function tick(){
 try{
  const s=await (await fetch('/api/stats')).json();
  rate.textContent=s.current?r(s.current.rate_out):'--';
  rin.textContent=s.current?(r(s.current.rate_in)+' tok/s'):'--';
  turn.textContent=s.turn?(r(s.turn.avg)+' / '+r(s.turn.peak)+' tok/s'):'--';
  tot.textContent=s.totals?(f(s.totals.out)+' / '+f(s.totals.in)):'--';
  meta.textContent=s.session+' · '+s.model+' · 本轮 '+s.turn.n+' 次调用 · 更新于 '+new Date().toLocaleTimeString();
  draw(s.calls||[]);
  renderWf(s.workflow||[]);
 }catch(e){meta.textContent='连接失败，重试中…'}
}
function draw(cs){
 const c=spark.getContext('2d');c.clearRect(0,0,880,140);
 if(!cs.length)return;
 const w=880,h=140,pad=6,n=cs.length,bw=Math.max(2,(w-2*pad)/n-1);
 const mx=Math.max(...cs.map(x=>x.rate),1);
 c.fillStyle='#3fa4ff';
 cs.forEach((x,i)=>{const bh=Math.max(2,x.rate/mx*(h-2*pad));c.fillRect(pad+i*((w-2*pad)/n),h-pad-bh,bw,bh)});
 c.fillStyle='#8a97a3';c.font='11px monospace';c.fillText('最近 '+n+' 次调用的出速率（峰值 '+r(mx)+' tok/s）',pad,12);
}
tick();setInterval(tick,1000);
</script></body></html>"""


IDLE_EXIT_SECONDS = float(os.environ.get("TOKEN_RATE_IDLE_EXIT") or 6 * 3600)


class _Calib:
    """chars/token 换算比分桶 EMA 存储（服务层流式瞬时速率用）。

    分桶键 = 调用模型 id（含通道信息，GLM/Anthropic 通道字符密度不同），
    每桶独立 EMA 并带有效样本数 n（渲染层可据此标注置信度）；查询回退链：
    模型桶(n>0) → default 桶（旧版文件迁移来的先验，n 可为 0）→ 默认 2.4。
    calib.json v2 结构：{"v":2,"buckets":{桶:{"ema":x,"n":N}},"chars_per_token":镜像}；
    兼容旧 v1 文件 {"chars_per_token":x}（迁入 default 桶）；写入时顶层镜像
    样本最多的桶，旧版代码读取不破。落盘走 tmp+rename 原子替换。"""

    DEFAULT = 2.4  # 中英混排默认值，几步调用后即被实测校准
    LO, HI = 0.5, 8.0  # 实测比值带（纯工具步字符 0 的样本不进此通道）

    def __init__(self, path):
        self.path = path
        self.buckets = {}
        self._load()

    def _load(self):
        data = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict) and data.get("v") == 2:
            for k, b in (data.get("buckets") or {}).items():
                if isinstance(b, dict) and isinstance(b.get("ema"), (int, float)) \
                        and self.LO <= b["ema"] <= self.HI:
                    self.buckets[str(k)] = {"ema": round(float(b["ema"]), 3),
                                            "n": max(int(b.get("n") or 0), 0)}
        else:  # 旧 v1：单值先验迁入 default 桶（n=0 表示未实测）
            v1 = data.get("chars_per_token") if isinstance(data, dict) else None
            if isinstance(v1, (int, float)) and self.LO <= v1 <= self.HI:
                self.buckets["default"] = {"ema": float(v1), "n": 0}

    def get(self, model):
        """返回 (换算比, 有效样本数, 桶键)。"""
        for k in (str(model or "") or "default", "default"):
            b = self.buckets.get(k)
            if b and b["n"] > 0:
                return b["ema"], b["n"], k
        b = self.buckets.get("default")
        if b:
            return b["ema"], b["n"], "default"
        return self.DEFAULT, 0, "default"

    def update(self, model, ratio):
        """一次实测校准：ratio = 消息 text/reasoning 字符 ÷ 调用输出 token。"""
        k = str(model or "default") or "default"
        b = self.buckets.setdefault(k, {"ema": None, "n": 0})
        if b["ema"] is None:  # 新桶以 default 先验（若有）起步
            seed = self.buckets.get("default")
            b["ema"] = seed["ema"] if seed else self.DEFAULT
        b["ema"] = min(max(round(b["ema"] * 0.7 + ratio * 0.3, 3), self.LO), self.HI)
        b["n"] += 1

    def save(self):
        mirror = self.DEFAULT
        best = max((b for b in self.buckets.values() if b["ema"] is not None),
                   key=lambda b: b["n"], default=None)
        if best:
            mirror = best["ema"]
        data = {"v": 2, "buckets": self.buckets, "chars_per_token": mirror}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)  # 原子替换：并发读不会见到半截 JSON
        except OSError:
            pass


def _new_stream_state(sid=None):
    """一个会话的流式采样基线（时间基准统一用单调钟，免疫系统时间跳变）。"""
    return {"sid": sid, "pid": None, "chars": 0, "ts": 0.0,
            "tps": None, "last_grow": 0.0}


def _sample_stream(sid, st, chars_per_token, now):
    """对会话 sid 最新 assistant 消息采样字符增量，更新瞬时 tok/s。

    采样状态 st 由调用方持有（按 scope 分区，互不串台）；消息切换即重置
    基线（新消息首见字符作起点）；超 3 秒无字符增长瞬时值作废（工具执行/
    等待时段）。chars_per_token 为该模型桶的校准比。"""
    try:
        sc = usage_db.stream_chars(sid)
    except Exception:
        sc = None
    if sc:
        mid, chars = sc
        if mid == st["pid"]:
            if chars > st["chars"] and now - st["ts"] >= 0.5:
                cps = (chars - st["chars"]) / (now - st["ts"])
                st["tps"] = round(cps / chars_per_token, 1)
                st["last_grow"] = now
            st["chars"], st["ts"] = chars, now
        else:  # 消息切换：基线重置，瞬时值留待 3 秒过期或新消息出增量
            st["sid"], st["pid"], st["chars"], st["ts"] = sid, mid, chars, now
    if now - st["last_grow"] > 3.0:
        st["tps"] = None
    return st["tps"]


def ui_log(msg):
    """把界面脚本文档的诊断写入 ui/server.log，便于界面结构变化时定位。"""
    try:
        os.makedirs(UI_DIR, exist_ok=True)
        with open(os.path.join(UI_DIR, "server.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def mode_serve(port, session_id):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    sid, path = resolve_session(session_id)
    turn_cache = usage_db.TurnCache(ttl=1.5)

    # ---- 并发模型（ThreadingHTTPServer 多线程）：采样收敛为单独周期任务，
    # 请求线程只读已发布快照、不做任何 DB 查询。锁只保护「周期任务 ↔ 请求
    # 线程」的交接（scope 登记、快照发布/读取、cid 缓存），采样计算与查询
    # 全部在周期线程锁外进行——请求持锁微秒级，竞争面收窄到最小。
    lock = threading.Lock()
    activity = {"at": time.monotonic()}
    SCOPE_ACTIVE_S = 60.0  # scope 无请求超过该秒数即停止刷新并淘汰

    scope_seen = {}  # scope -> 最近请求的单调时刻（周期任务照此刷新/淘汰）
    live_cache = {}  # scope -> {"ok_at","at","err","data"}（成功时间与错误分开记）
    wf_cache = {}    # scope -> 同上，data={"runs","subs","sub_agg_instant"}

    # ---- 流式瞬时速率采样：chars/token 比按模型分桶校准（_Calib），采样状态
    # 按 scope 分区——多窗口/自动化并存时互不串台、清理只作用于本 scope，
    # 切换会话时瞬时值清零重测；时间基准统一单调钟。以下状态仅周期任务线程
    # 读写，不参与锁竞争。
    streams = {}      # scope -> 主轮采样基线
    wf_streams = {}   # scope -> {子会话 id: 基线}（工作流 actor）
    sub_streams = {}  # scope -> {子会话 id: 基线}（普通子代理）

    # ---- 流式校准：按 assistant_message_id 配对「完成调用输出 token ↔ 该
    # 消息最终 text/reasoning 字符」（真实库 3457/3457 全带该 id）。调用完成
    # 即落行，轮结束后最后一条调用同样可查——校准不再依赖轮仍 live。工具调用
    # 参数不在 text/reasoning 部件，天然不计入字符分子；纯工具步（字符 0）
    # 直接跳过。按调用 id 去重保证幂等。同样仅周期任务线程读写。
    calib = _Calib(os.path.join(UI_DIR, "calib.json"))
    calib_seen = {}     # 调用 id -> 处理完的单调时刻（去重，10 分钟窗口）
    calib_pending = {}  # 调用 id -> {"chars","tries","t0"}（两遍采样确认消息定型）

    def _refresh_live(scope):
        """刷新一个 scope 的实时轮数据（周期任务线程内调用）。

        失败保留旧数据但 ok_at 不动（数据年龄如实增长），错误记录在 err——
        陈旧上限由响应侧判定，超限即置空，绝不把旧值当新值下发。"""
        sess_q = None if scope == GLOBAL_SCOPE else scope
        try:
            lv = usage_db.live_turn(session_id=sess_q)
            err = None
        except Exception as exc:
            lv, err = None, f"{type(exc).__name__}: {exc}"
        if lv and lv.get("session_id"):
            st = streams.get(scope)
            if st is None or st["sid"] != lv["session_id"]:
                st = _new_stream_state(lv["session_id"])  # 会话切换：基线清零重测
                streams[scope] = st
            model = (lv.get("models") or [None])[-1]
            v, n, bkey = calib.get(model)
            lv["instant_tps"] = _sample_stream(lv["session_id"], st, v, time.monotonic())
            lv["calib"] = v
            lv["calib_n"] = n        # 该换算比的有效校准样本数
            lv["calib_bucket"] = bkey
        now = time.monotonic()
        prev = live_cache.get(scope)
        live_cache[scope] = {
            "at": now,
            "ok_at": now if err is None else (prev or {}).get("ok_at", 0.0),
            "err": err,
            "data": lv if err is None else (prev or {}).get("data"),
        }

    def _refresh_wfu(scope):
        """刷新一个 scope 的工作流 + 普通子代理块（周期任务线程内调用）。"""
        sess_q = None if scope == GLOBAL_SCOPE else scope
        now = time.monotonic()
        try:
            runs = usage_db.workflow_live(parent_session_id=sess_q) or []
            subs = usage_db.subagent_live(parent_session_id=sess_q) or []
            err = None
        except Exception as exc:
            runs, subs, err = [], [], f"{type(exc).__name__}: {exc}"
        if err is None:
            # 工作流 actor 瞬时采样：状态按本 scope 分区，清理只清本 scope
            # 跟踪器——两个窗口交替请求不再互清对方基线
            st_map = wf_streams.setdefault(scope, {})
            seen = set()
            for run in runs:
                agg = 0.0
                for a in run.get("per_actor", []):
                    sid_a = a.get("session_id")
                    if not sid_a:
                        continue
                    seen.add(sid_a)
                    st = st_map.setdefault(sid_a, _new_stream_state(sid_a))
                    v = calib.get(a.get("model"))[0]
                    a["instant_tps"] = _sample_stream(sid_a, st, v, now)
                    if a.get("active") and a["instant_tps"]:
                        agg += a["instant_tps"]
                # agg_instant = 并行瞬时合计（各活跃子代理瞬时值相加），与
                # tps/decode_ref_tps（调用均值）是两种口径，渲染层勿混用
                run["agg_instant"] = round(agg, 1) if agg else None
            for sid_a in [k for k in st_map if k not in seen]:
                st_map.pop(sid_a, None)  # 运行结束/宽限过期：只清本 scope
            # 普通子代理（Agent 工具）瞬时采样：批次行同构注入 instant_tps
            sb_map = sub_streams.setdefault(scope, {})
            sub_seen = set()
            sub_agg = 0.0
            for b in subs:
                sid_b = b.get("session_id")
                if not sid_b:
                    continue
                sub_seen.add(sid_b)
                st = sb_map.setdefault(sid_b, _new_stream_state(sid_b))
                inst = None
                if b.get("active"):
                    inst = _sample_stream(sid_b, st, calib.get(b.get("model"))[0], now)
                b["instant_tps"] = inst
                if inst:
                    sub_agg += inst
            for sid_b in [k for k in sb_map if k not in sub_seen]:
                sb_map.pop(sid_b, None)
            data = {"runs": runs, "subs": subs,
                    "sub_agg_instant": round(sub_agg, 1) if sub_agg else None}
        else:
            data = None
        prev = wf_cache.get(scope)
        wf_cache[scope] = {
            "at": now,
            "ok_at": now if err is None else (prev or {}).get("ok_at", 0.0),
            "err": err,
            "data": data if err is None else (prev or {}).get("data"),
        }

    def _calibrate():
        """流式校准（周期任务线程内调用）：扫最近完成调用，按消息配对。

        幂等：按调用 id 去重（seen 窗口 10 分钟 > 查询窗 5 分钟，进程内不重复
        处理）；服务重启后 seen 清空会重放窗内调用一次——彼时消息字符早已定型，
        重复的只是同一定型比值对 EMA 的再收敛，影响可忽略。定型判定：连续两遍
        采样字符相同（消息不再增长）才落校准，超次数用最新值兜底。消息暂查不到
        （未落库/瞬时库错误）不记 seen，进 120 秒观察窗重试，到期才放弃——首查
        失败不让该调用永远失去校准资格。"""
        now = time.monotonic()

        def _settle(cid_, chars, out, model_id):
            """定型收账：带内比值落校准，之后无论结果都记 seen 并清 pending。"""
            if chars and _Calib.LO <= chars / out <= _Calib.HI:
                calib.update(model_id, chars / out)
                calib.save()
            calib_seen[cid_] = now
            calib_pending.pop(cid_, None)

        try:
            calls = usage_db.recent_completed_calls(age_s=300, limit=40)
        except Exception:
            calls = []  # 查询失败（连接失败等）不中断清账：到期 pending 仍要独立结算
        round_ids = set()
        for c in calls:
            cid_ = c["id"]
            round_ids.add(cid_)
            if cid_ in calib_seen:
                continue
            out = c["output_tokens"] or 0
            mid = c["assistant_message_id"]
            if not mid or out < 50:  # 微小输出不具校准价值
                calib_seen[cid_] = now
                continue
            p = calib_pending.get(cid_)
            if p is None:  # 首见：无论查不查得到都先观察，绝不直接记 seen
                try:
                    sc = usage_db.stream_chars(c["session_id"], message_id=mid)
                except Exception:
                    sc = None  # 瞬时 SQLite 错误与「消息未落库」同走 pending 重试，
                    #           不记 seen——首查失败不该让该调用永远失去校准资格
                calib_pending[cid_] = {"chars": sc[1] if sc else None, "tries": 1,
                                       "t0": now, "out": out, "model_id": c["model_id"],
                                       "sid": c["session_id"], "mid": mid}
                continue
            if now - p["t0"] >= 120:
                continue  # 到期条目统一交循环后的清账处理（含已滑出查询窗的）
            try:
                sc = usage_db.stream_chars(c["session_id"], message_id=mid)
            except Exception:
                sc = None
            if sc is None:
                continue  # 暂查不到（消息未落库/库忙）：留在 pending 等下一拍
            chars = sc[1]
            if p["chars"] is None:  # 首次见到字符：记基线，下一拍再判定型
                p["chars"], p["tries"] = chars, p["tries"] + 1
                continue
            settled = p["chars"] == chars or p["tries"] >= 10
            if not settled:
                p["chars"], p["tries"] = chars, p["tries"] + 1
                continue  # 消息仍在写：继续等。chars 0→>0 亦属此列——调用
                # completed_at 落库可能早于 assistant 消息 part 写入字符，
                # 首拍 0 不能定性为纯工具步，观察两遍再下结论
            _settle(cid_, chars, p["out"], p["model_id"])
        # 统一清账：到期 pending 无论本轮是否还出现在查询结果里都做最后兜底——
        # 高调用量挤出 40 条窗口或超出 300s 年龄后主循环不再见到该调用，
        # 不独立清账就会永久滞留 pending（终审实测 127s 仍卡住）
        for cid_, p in list(calib_pending.items()):
            if now - p["t0"] < 120:
                continue
            chars = p["chars"]
            if chars is None and p.get("mid"):
                try:
                    sc = usage_db.stream_chars(p["sid"], message_id=p["mid"])
                    chars = sc[1] if sc else None
                except Exception:
                    chars = None  # 最后兜底也查不到：放弃，记 seen 防永久滞留
            _settle(cid_, chars, p["out"], p["model_id"])
        for k in [k for k, t in calib_seen.items() if now - t > 600]:
            calib_seen.pop(k, None)

    def sampler():
        """采样周期任务：每秒刷新活跃 scope 的实时/工作流块并做流式校准。

        与请求线程的交接只在「读活跃 scope 列表 / 发布快照」两处拿锁。"""
        while True:
            t0 = time.monotonic()
            try:
                with lock:
                    scopes = [s for s in scope_seen if t0 - scope_seen[s] <= SCOPE_ACTIVE_S]
                for scope in scopes:
                    _refresh_live(scope)
                    _refresh_wfu(scope)
                _calibrate()
                with lock:
                    dead = [s for s in scope_seen if t0 - scope_seen[s] > SCOPE_ACTIVE_S]
                    for s in dead:
                        scope_seen.pop(s, None)
                        live_cache.pop(s, None)
                        wf_cache.pop(s, None)
                for s in dead:  # 采样状态归周期线程独占，锁外清
                    streams.pop(s, None)
                    wf_streams.pop(s, None)
                    sub_streams.pop(s, None)
            except Exception as exc:
                ui_log(f"[sampler] {type(exc).__name__}: {exc}")
            time.sleep(max(0.2, 1.0 - (time.monotonic() - t0)))

    # 渲染层报来的 cid（本窗口 DOM 的 data-turn-id）→ 会话 id。
    # 未解析（消息尚未落库/前缀形态变化）只负缓存 5 秒，绝不回退全局——
    # 多窗口并存时回退全局最新轮必然串显；全局查询只留 /live?global=1
    # 显式调试入口（以及 CLI 的 live 命令）。
    cid_cache = {}
    live_dbg_state = {"last": None}  # /live cid 诊断节流（serve 闭包，方法内可见）

    def _scope_of_cid(cid):
        if not cid:
            return None
        now = time.monotonic()
        with lock:
            hit = cid_cache.get(cid)
        if hit:
            sid_c, at = hit
            if now - at <= (5.0 if sid_c is None else 3600.0):
                return sid_c
        sid_c = usage_db.session_of_message(cid)
        with lock:
            cid_cache[cid] = (sid_c, now)
            if len(cid_cache) > 64:  # 防御性裁剪：按登记时刻留最近一半
                for k in sorted(cid_cache, key=lambda k: cid_cache[k][1])[:-32]:
                    cid_cache.pop(k, None)
        return sid_c

    def _live_response(scope, pending):
        """组装 /live 响应（请求线程只读快照，带数据年龄与 stale 标记）。"""
        now = time.monotonic()
        with lock:
            le = dict(live_cache.get(scope) or {})
            we = dict(wf_cache.get(scope) or {})

        def _meta(e):
            ok_at = e.get("ok_at") or 0.0
            age_ms = int((now - ok_at) * 1000) if ok_at else None
            stale = ok_at == 0 or (now - ok_at) > LIVE_MAX_STALE_S
            return age_ms, bool(stale), e.get("err")

        age, stale, err = _meta(le)
        wage, wstale, werr = _meta(we)
        wdata = {} if wstale else (we.get("data") or {})
        return {
            "schema": 2,
            "live": None if stale else le.get("data"),
            "live_pending": bool(pending),  # cid 已报但尚未解析到会话：重试即可
            "live_scope": scope,
            "live_age_ms": age,             # 数据年龄（成功刷新距今）
            "live_stale": stale,            # 超陈旧上限：live 已置空，勿用旧值
            "live_error": err,
            "workflow": wdata.get("runs") or [],
            "workflow_age_ms": wage,
            "workflow_stale": wstale,
            "workflow_error": werr,
            "subagents": wdata.get("subs") or [],
            "sub_agg_instant": wdata.get("sub_agg_instant"),
            "legend": LIVE_LEGEND,
        }

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            activity["at"] = time.monotonic()
            try:
                if self.path.startswith("/turns"):
                    limit = 800
                    if "limit=" in self.path:
                        try:
                            limit = min(int(self.path.split("limit=")[1].split("&")[0]), 2000)
                        except ValueError:
                            pass
                    # 折叠结果带 meta（数据年龄 / stale / 最近错误）：连续查询失败
                    # 超过陈旧上限后 turns 为空列表，绝不无限保留旧值冒充新鲜
                    meta = turn_cache.get_meta()
                    return self._send(json.dumps(
                        {"turns": meta["turns"][:limit], "schema": 6,
                         "meta": {"age_ms": meta["age_ms"], "stale": meta["stale"],
                                  "error": meta["error"]}},
                        ensure_ascii=False))
                if self.path.startswith("/live"):
                    # 进行中轮次的实时数据（周期任务每秒刷新，请求只读快照）。
                    # cid = 渲染层上报的本窗口 data-turn-id（user 消息 id），服务端
                    # 解析出会话后整套数据（实时行 + workflow + 子代理块）都限定在
                    # 该会话内，多窗口/自动化并存时互不串显；cid 未解析返回
                    # 「待解析」状态（live_pending），不回退全局；全局查询只留
                    # ?global=1 显式调试入口。
                    from urllib.parse import unquote
                    cid = ""
                    if "cid=" in self.path:
                        try:
                            cid = unquote(self.path.split("cid=", 1)[1].split("&")[0])
                        except ValueError:
                            cid = ""
                    scope = GLOBAL_SCOPE if "global=1" in self.path else _scope_of_cid(cid)
                    pending = bool(cid) and scope is None
                    if scope is not None:
                        with lock:
                            scope_seen[scope] = time.monotonic()
                    # 诊断（按变化节流）：渲染层上报的 cid → 解析出的会话。宿主升级
                    # 改动 DOM 的 data-turn-id 语义时，靠它定位实时行不显示的根因
                    _dbg = f"{cid[:48]}→{scope}"
                    if _dbg != live_dbg_state["last"]:
                        live_dbg_state["last"] = _dbg
                        ui_log(f"[live-cid] {_dbg}")
                    return self._send(json.dumps(_live_response(scope, pending),
                                                 ensure_ascii=False))
                if self.path.startswith("/healthz"):
                    return self._send("ok", "text/plain")
                if self.path.startswith("/diag"):
                    from urllib.parse import unquote
                    msg = self.path.split("msg=", 1)[1] if "msg=" in self.path else ""
                    ui_log(f"[inject] {unquote(msg)[:1200]}")
                    return self._send("ok", "text/plain")
                if self.path.startswith("/api/stats"):
                    cur_sid, p = (resolve_session("auto") if not sid else (sid, path))
                    body = {"error": "no-data"}
                    if p:
                        calls, totals, state = load_calls(cur_sid, p)
                        s = stats_for(calls, totals, cur_sid, state.get("turn_agg"))
                        if s:
                            s["calls"] = [
                                {"t": c["t1"], "dur": round(c["dur"], 1), "out": c["out"],
                                 "rate": round(c["out"] / c["dur"], 1) if c["dur"] > 0 else 0}
                                for c in calls[-64:]
                            ]
                            body = s
                    # 仪表盘工作流面板：限定在仪表盘所跟踪会话内（只读快照，
                    # 超陈旧上限置空，与 /live 同一判定）。仪表盘 JS 只拉本
                    # 端点（PAGE 内 fetch('/api/stats')），必须在此登记 scope，
                    # 否则采样器永远不刷新该会话、面板恒为空
                    if cur_sid:
                        with lock:
                            scope_seen[cur_sid] = time.monotonic()
                        with lock:
                            we = dict(wf_cache.get(cur_sid) or {})
                    else:
                        we = {}
                    w_ok = we.get("ok_at") or 0.0
                    wstale = w_ok == 0.0 or (time.monotonic() - w_ok) > LIVE_MAX_STALE_S
                    wdata = {} if wstale else (we.get("data") or {})
                    body["workflow"] = wdata.get("runs") or []
                    body["subagents"] = wdata.get("subs") or []
                    body["legend"] = LIVE_LEGEND
                    return self._send(json.dumps(body, ensure_ascii=False))
                return self._send(PAGE, "text/html; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # 服务端绝不因单次请求异常退出
                ui_log(f"[serve] {type(exc).__name__}: {exc}")

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)

    def idle_watch():
        while True:
            time.sleep(600)
            if time.monotonic() - activity["at"] > IDLE_EXIT_SECONDS:
                ui_log(f"[serve] 空闲超过 {IDLE_EXIT_SECONDS:.0f}s，自动退出")
                srv.shutdown()
                return

    threading.Thread(target=idle_watch, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    print(f"⚡ token-rate 本地服务已启动：http://127.0.0.1:{port}"
          f"（仪表盘 / ；页脚数据 /turns ；会话 {sid or 'auto'}）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- 界面页脚（可选 UI 模块）

# 应用路径可用环境变量覆盖（预演测试、非标准安装位置）
APP_BUNDLE = os.environ.get("TOKEN_RATE_APP") or "/Applications/ZCode.app"
ASAR = os.path.join(APP_BUNDLE, "Contents", "Resources", "app.asar")
UNPACKED = os.path.join(APP_BUNDLE, "Contents", "Resources", "app.asar.unpacked")
BAK = ASAR + ".token-rate-bak"
BAK_UNPACKED = UNPACKED + ".token-rate-bak"
STAGE_JS = os.path.join(UI_DIR, "inject.js")
ENABLED_FLAG = os.path.join(UI_DIR, "enabled")
PORT_FILE = os.path.join(UI_DIR, "port")
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INJECT_SRC = os.path.join(PLUGIN_ROOT, "ui", "inject.js")
INNER_HTML = "out/renderer/index.html"
MARKER = "token-rate-hud/ui/inject.js"
UNPACK_GLOB = "{**/*.node,**/spawn-helper}"
DEFAULT_PORT = 7864


def _run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _asar(cmd_dir):
    """返回可用的 asar 命令前缀（npx 缓存的 @electron/asar）。"""
    r = _run(["npx", "--yes", "@electron/asar", "--version"], cwd=cmd_dir)
    if r.returncode != 0:
        return None
    return ["npx", "--yes", "@electron/asar"]


def _marker_in(asar_path, workdir):
    """检查 index.html 是否已含注入标记。"""
    out_dir = os.path.join(workdir, "probe")
    os.makedirs(out_dir, exist_ok=True)
    r = _run(["npx", "--yes", "@electron/asar", "extract-file", asar_path, INNER_HTML], cwd=out_dir)
    p = os.path.join(out_dir, os.path.basename(INNER_HTML))
    if not os.path.exists(p):
        return None  # 探测失败
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return MARKER in f.read()
    finally:
        try:
            os.remove(p)
        except OSError:
            pass


def _quarantined():
    r = _run(["xattr", "-l", APP_BUNDLE])
    return "quarantine" in (r.stdout or "")


def _write_stage_js(port):
    os.makedirs(UI_DIR, exist_ok=True)
    with open(INJECT_SRC, encoding="utf-8") as f:
        src = f.read()
    src = src.replace("__PORT__", str(port))
    with open(STAGE_JS, "w", encoding="utf-8") as f:
        f.write(src)


def _unpacked_manifest(root):
    items = {}
    for base, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(base, fn)
            items[os.path.relpath(p, root)] = os.path.getsize(p)
    return items


def _swap_unpacked(new_dir):
    """把重打包产出的 .unpacked 换到位；内容与现状一致则不动。"""
    if not os.path.isdir(new_dir):
        return "无（新包未产生 unpacked 目录）"
    new_manifest = _unpacked_manifest(new_dir)
    old_manifest = _unpacked_manifest(UNPACKED) if os.path.isdir(UNPACKED) else {}
    if new_manifest == old_manifest:
        return "内容一致，保持原目录不动"
    aside = UNPACKED + ".token-rate-old"
    if os.path.exists(aside):
        import shutil as _sh
        _sh.rmtree(aside, ignore_errors=True)
    if os.path.isdir(UNPACKED):
        os.rename(UNPACKED, aside)
    os.rename(new_dir, UNPACKED)
    import shutil as _sh
    _sh.rmtree(aside, ignore_errors=True)
    return f"已替换（{len(new_manifest)} 个文件）"


def _parse_asar_header(asar_path):
    """解析 asar 头部，返回 {路径: (size, unpacked)}。

    头部格式（Chromium pickle）：[4 字节常量][pickle 大小][JSON 长度][JSON 起始于第 16 字节]。
    新版 @electron/asar 会对相同内容去重（多个条目共享偏移），故不校验偏移只校验尺寸。
    """
    import struct
    with open(asar_path, "rb") as f:
        head = f.read(16)
        if len(head) < 16:
            return None
        json_len = struct.unpack("<I", head[12:16])[0]
        hdr = json.loads(f.read(json_len))
    files = {}

    def walk(node, prefix=""):
        for name, v in node.get("files", {}).items():
            p = prefix + "/" + name
            if "files" in v:
                walk(v, p)
            else:
                files[p] = (v.get("size", 0), bool(v.get("unpacked")))

    walk(hdr)
    return files


def _verify_pack(new_asar, workdir):
    """重打包后自检：全量解包，逐文件比对头部声明的尺寸。

    能抓住打包器版本变化导致的截断/缺内容（这类问题会让 ZCode 读取到空文件）。
    """
    import shutil as _sh
    dest = os.path.join(workdir, "verify_extract")
    _sh.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    r = _run(["npx", "--yes", "@electron/asar", "extract", new_asar, dest], cwd=workdir)
    if r.returncode != 0:
        return False, f"解包自检失败：{(r.stderr or r.stdout)[:200]}"
    expect = _parse_asar_header(new_asar)
    if not expect:
        return False, "自检失败：无法解析新包头部"
    missing = bad = 0
    for path, (size, unpacked) in expect.items():
        if unpacked:
            continue
        fp = os.path.join(dest, path.lstrip("/"))
        if not os.path.exists(fp):
            missing += 1
        elif os.path.getsize(fp) != size:
            bad += 1
    if missing or bad:
        return False, f"自检失败：缺 {missing} 个、尺寸不符 {bad} 个"
    return True, f"自检通过（{sum(1 for _, u in expect.values() if not u)} 个内联文件尺寸全部一致）"


def ui_install(port, skip_verify=False):
    if not os.path.exists(ASAR):
        print(f"✗ 找不到 {ASAR}，界面页脚模块仅支持 macOS 桌面版 ZCode")
        return 1
    if _quarantined():
        print("⚠️  ZCode.app 带 quarantine 隔离标记：改包后 Gatekeeper 可能阻止启动。")
        print("    如遇无法启动，先执行 ui-uninstall 还原；仍要尝试可先去掉隔离标记。")

    _write_stage_js(port)
    with open(PORT_FILE, "w") as f:
        f.write(str(port))
    if not os.path.exists(ENABLED_FLAG):
        open(ENABLED_FLAG, "w").close()
    print(f"① 界面脚本已就位：{STAGE_JS}（端口 {port}）")

    work = os.path.join(STATE_DIR, "asar-work")
    import shutil as _sh
    _sh.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    marker = _marker_in(ASAR, work)
    if marker is True:
        print("② 当前 app.asar 已含注入标记，无需重打包。")
        print(f"✅ 完成。数据服务常驻：{launchd_install(port)}")
        print("   请完全退出 ZCode（Cmd+Q）再打开；数据服务由 launchd 保活、开机自启。")
        return 0

    if _asar(work) is None:
        print("✗ 无法调用 @electron/asar（npx 不可用或缺少网络缓存）")
        return 1

    if not os.path.exists(BAK):
        print(f"③ 备份原包 → {BAK}")
        _sh.copy2(ASAR, BAK)
        if os.path.isdir(UNPACKED) and not os.path.exists(BAK_UNPACKED):
            _sh.copytree(UNPACKED, BAK_UNPACKED, symlinks=True)
    else:
        print("③ 已有备份，跳过备份步骤")

    print("④ 解包 app.asar ……")
    r = _run(["npx", "--yes", "@electron/asar", "extract", ASAR, "unpacked"], cwd=work)
    idx = os.path.join(work, "unpacked", *INNER_HTML.split("/"))
    if r.returncode != 0 or not os.path.exists(idx):
        print(f"✗ 解包失败：{(r.stderr or r.stdout)[:400]}")
        return 1

    print("⑤ 注入脚本标签 ……")
    with open(idx, encoding="utf-8") as f:
        html_text = f.read()
    if MARKER not in html_text:
        tag = f'    <script defer src="file://{STAGE_JS}"></script>\n'
        if "  </head>" in html_text:
            html_text = html_text.replace("  </head>", tag + "  </head>", 1)
        elif "</head>" in html_text:
            html_text = html_text.replace("</head>", tag + "</head>", 1)
        else:
            print("✗ index.html 结构异常（找不到 </head>），未做任何修改")
            return 1
        with open(idx, "w", encoding="utf-8") as f:
            f.write(html_text)

    print("⑥ 重打包 ……（约 10-60 秒）")
    new_asar = os.path.join(work, "app.asar.patched")
    r = _run(["npx", "--yes", "@electron/asar", "pack", "unpacked", "app.asar.patched",
              "--unpack", UNPACK_GLOB], cwd=work)
    if r.returncode != 0 or not os.path.exists(new_asar):
        print(f"✗ 重打包失败：{(r.stderr or r.stdout)[:400]}")
        return 1

    print("⑦ 校验新包 ……")
    probe = os.path.join(work, "verify")
    os.makedirs(probe, exist_ok=True)
    if _marker_in(new_asar, probe) is not True:
        print("✗ 新包校验未通过（注入标记缺失），已放弃替换，原包未动")
        return 1
    ok, msg = _verify_pack(new_asar, work)
    print(f"   {msg}")
    if not ok:
        print("✗ 内容完整性自检未通过，已放弃替换，原包未动")
        return 1

    print("⑧ 替换 app.asar（原子替换，运行中的 ZCode 不受影响）……")
    os.replace(new_asar, ASAR)
    note = _swap_unpacked(new_asar + ".unpacked")
    print(f"   app.asar.unpacked：{note}")

    if not skip_verify:
        sig = _run(["codesign", "--verify", APP_BUNDLE])
        if sig.returncode == 0:
            print("   签名校验：通过")
        else:
            print("   签名校验：未通过（改包后属正常现象；无 quarantine 隔离标记时不影响启动）")

    _sh.rmtree(work, ignore_errors=True)  # 清理临时解包目录（约 600MB）
    print(f"⑨ 数据服务常驻：{launchd_install(port)}")
    print("✅ 完成。请完全退出 ZCode（Cmd+Q）再打开，每条回答下方即出现统计行。")
    print(f"   还原：python3 {os.path.abspath(__file__)} ui-uninstall")
    return 0


def ui_uninstall():
    """从当前 app.asar 剔除注入标签并重打包。

    注意：绝不自动用 .token-rate-bak 备份还原——该备份可能属于旧版本 ZCode
    （应用自动升级后 asar 与二进制不匹配会导致启动异常）。剔除标签的干净重打包
    在任何版本下都正确；备份仅留作应急手动手段。"""
    work = os.path.join(STATE_DIR, "asar-work")
    import shutil as _sh
    if not os.path.exists(ASAR):
        print("✗ 找不到 app.asar，无法处理")
        return 1
    _sh.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    r = _run(["npx", "--yes", "@electron/asar", "extract", ASAR, "unpacked"], cwd=work)
    idx = os.path.join(work, "unpacked", *INNER_HTML.split("/"))
    if r.returncode != 0 or not os.path.exists(idx):
        print(f"✗ 解包失败：{(r.stderr or r.stdout)[:300]}")
        return 1
    with open(idx, encoding="utf-8") as f:
        lines = f.readlines()
    kept = [ln for ln in lines if MARKER not in ln]
    if len(kept) == len(lines):
        print("当前包不含注入标记，无需重打包")
    else:
        with open(idx, "w", encoding="utf-8") as f:
            f.writelines(kept)
        print("重打包（剔除注入标签）……（约 10-60 秒）")
        r = _run(["npx", "--yes", "@electron/asar", "pack", "unpacked", "app.asar.clean",
                  "--unpack", UNPACK_GLOB], cwd=work)
        clean = os.path.join(work, "app.asar.clean")
        if r.returncode != 0 or not os.path.exists(clean):
            print(f"✗ 重打包失败：{(r.stderr or r.stdout)[:300]}")
            return 1
        probe = os.path.join(work, "probe")
        os.makedirs(probe, exist_ok=True)
        if _marker_in(clean, probe) is not False:
            print("✗ 新包仍含标记（异常），已放弃替换，原包未动")
            return 1
        os.replace(clean, ASAR)
        _swap_unpacked(clean + ".unpacked")
        print("✅ 已剔除注入标签（完全退出并重开 ZCode 后页脚消失）")
    if os.path.exists(ENABLED_FLAG):
        os.remove(ENABLED_FLAG)
    for p in (STAGE_JS, PORT_FILE):
        try:
            os.remove(p)
        except OSError:
            pass
    print(f"数据服务常驻：{launchd_remove()}")
    _stop_server()
    _sh.rmtree(work, ignore_errors=True)
    if os.path.exists(BAK):
        print(f"ℹ️  应急备份仍在：{BAK}（仅在应用无法启动时手动还原；若 ZCode 已升级请勿使用）")
    return 0


def _server_port():
    try:
        with open(PORT_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return DEFAULT_PORT


def _server_alive(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _ensure_server(port, quiet=False):
    """界面页脚依赖的本地数据服务；未运行则后台拉起（不阻塞 hook）。"""
    if _server_alive(port):
        return "已在运行"
    if os.path.exists(LAUNCHD_PLIST):  # launchd 常驻：交给 launchd 拉起，避免双进程抢端口
        _run(["launchctl", "kickstart", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])
        return "已请求 launchd 拉起"
    os.makedirs(UI_DIR, exist_ok=True)
    log_p = os.path.join(UI_DIR, "server.log")
    cmd = [sys.executable, os.path.abspath(__file__), "serve", "--port", str(port)]
    try:
        with open(log_p, "a") as log:
            subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd="/tmp")
        msg = f"已后台拉起（端口 {port}，日志 {log_p}）"
    except OSError as exc:
        msg = f"拉起失败：{exc}"
    if not quiet:
        print(msg)
    return msg


def _stop_server():
    r = _run(["pgrep", "-f", "tokrate.py serve"])
    pids = [p for p in (r.stdout or "").split() if p.isdigit()]
    if pids:
        _run(["kill"] + pids)
        return f"已停止 {len(pids)} 个服务进程"
    return "无运行中的服务"


# ---- launchd 常驻：数据服务保活 + 开机自启（ui-install 装，ui-uninstall 卸） ----
# 背景：服务原有「空闲 6h 自杀 + SessionStart 钩子拉起」的设计，但实测该钩子在
# ZCode 升级后不再可靠触发，服务一旦空闲退出就永远起不来，页脚随之消失。
LAUNCHD_LABEL = "com.zcode.token-rate-hud"
LAUNCHD_PLIST = os.path.join(HOME, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")
STAGE_LIB = os.path.join(UI_DIR, "lib")


def _launchd_domain():
    return f"gui/{os.getuid()}"


def _stage_lib():
    """把 lib 两件套复制到 UI_DIR/lib，给 launchd 一个跨插件版本稳定的运行路径。"""
    import shutil as _sh
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(STAGE_LIB, exist_ok=True)
    _sh.copy2(os.path.join(here, "usage_db.py"), os.path.join(STAGE_LIB, "usage_db.py"))
    _sh.copy2(os.path.join(here, "tokrate.py"), os.path.join(STAGE_LIB, "tokrate.py"))


def launchd_install(port):
    """写入 LaunchAgent 并加载（KeepAlive 保活 + RunAtLoad 开机自启）。

    常驻进程通过 TOKEN_RATE_IDLE_EXIT=999999999 关闭空闲自杀——保活与空闲退出
    并存只会造成退出/重启的无意义循环。"""
    try:
        _stage_lib()
        entry = os.path.join(STAGE_LIB, "tokrate.py")
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
            ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'  <key>Label</key><string>{LAUNCHD_LABEL}</string>\n'
            '  <key>ProgramArguments</key>\n  <array>\n'
            f'    <string>{sys.executable}</string>\n'
            f'    <string>{entry}</string>\n'
            '    <string>serve</string>\n'
            '    <string>--port</string>\n'
            f'    <string>{port}</string>\n'
            '  </array>\n'
            '  <key>EnvironmentVariables</key><dict>\n'
            '    <key>TOKEN_RATE_IDLE_EXIT</key><string>999999999</string>\n'
            '  </dict>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><true/>\n'
            f'  <key>StandardOutPath</key><string>{os.path.join(UI_DIR, "server.log")}</string>\n'
            f'  <key>StandardErrorPath</key><string>{os.path.join(UI_DIR, "server.log")}</string>\n'
            '  <key>WorkingDirectory</key><string>/tmp</string>\n'
            '  <key>ProcessType</key><string>Background</string>\n'
            '</dict></plist>\n'
        )
        os.makedirs(os.path.dirname(LAUNCHD_PLIST), exist_ok=True)
        with open(LAUNCHD_PLIST, "w", encoding="utf-8") as f:
            f.write(plist)
        _run(["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])  # 已加载则先卸（忽略失败）
        r = _run(["launchctl", "bootstrap", _launchd_domain(), LAUNCHD_PLIST])
        if r.returncode != 0:
            r = _run(["launchctl", "load", "-w", LAUNCHD_PLIST])  # 旧版兜底
        if r.returncode != 0:
            return f"✗ launchd 加载失败：{(r.stderr or r.stdout)[:200]}"
        return f"已装并加载（{LAUNCHD_PLIST}）"
    except OSError as exc:
        return f"✗ 安装失败：{exc}"


def launchd_remove():
    import shutil as _sh
    if not os.path.exists(LAUNCHD_PLIST) and not os.path.isdir(STAGE_LIB):
        return "未安装"
    _run(["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])
    _run(["launchctl", "unload", "-w", LAUNCHD_PLIST])  # 旧版兜底，忽略失败
    try:
        os.remove(LAUNCHD_PLIST)
    except OSError:
        pass
    _sh.rmtree(STAGE_LIB, ignore_errors=True)
    return "已卸载"


def launchd_state():
    """返回 (plist 存在, launchd 已加载, pid)。"""
    plist_ok = os.path.exists(LAUNCHD_PLIST)
    r = _run(["launchctl", "print", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])
    loaded = r.returncode == 0
    pid = None
    if loaded:
        for ln in (r.stdout or "").splitlines():
            if ln.strip().startswith("pid ="):
                pid = ln.split("=", 1)[1].strip()
                break
    return plist_ok, loaded, pid


def ui_status():
    port = _server_port()
    marker = None
    if os.path.exists(ASAR):
        probe = os.path.join(STATE_DIR, "probe")
        os.makedirs(probe, exist_ok=True)
        marker = _marker_in(ASAR, probe)
    print("⚡ token-rate-hud 界面页脚状态")
    print(f"  ZCode 应用      ：{'存在' if os.path.exists(ASAR) else '缺失'}（{APP_BUNDLE}）")
    print(f"  注入标记        ：{'已注入' if marker else ('未注入' if marker is False else '探测失败')}")
    print(f"  原始包备份      ：{'有 ' + BAK if os.path.exists(BAK) else '无'}")
    print(f"  界面脚本        ：{'就位 ' + STAGE_JS if os.path.exists(STAGE_JS) else '未生成'}")
    print(f"  启用标记        ：{'有' if os.path.exists(ENABLED_FLAG) else '无'}")
    print(f"  数据服务        ：{'运行中' if _server_alive(port) else '未运行'}（端口 {port}）")
    plist_ok, loaded, pid = launchd_state()
    print(f"  launchd 常驻    ：{'已装' if plist_ok else '未装'}（launchd {'已加载' if loaded else '未加载'}，pid {pid or '-'}）")
    print(f"  用量库          ：{'可用' if usage_db.db_available() else '缺失'}（{usage_db.DB_PATH}）")
    if marker is False:
        print("  → 若刚升级过 ZCode，重跑 ui-install 即可恢复页脚")
    return 0


def mode_footer(limit):
    """调试：打印界面页脚的数据（与注入脚本拿到的 /turns 一致）。"""
    turns = usage_db.fold_turns(limit=limit)
    if not turns:
        print("（暂无用量的轮次数据）")
        return 0
    print("（口径：端到端=整轮墙钟含首包等待与工具执行；Decode=首输出后调用均值，"
          "两者勿混称）")
    print(f"{'完成时间':<20}{'用时':>9}{'首token':>9}{'端到端(含首包)':>14}{'剔等待':>8}{'Decode':>9}{'出tok':>8}  状态/模型")
    for t in turns:
        ts = datetime.fromtimestamp((t["end_ms"] or 0) / 1000).strftime("%m-%d %H:%M:%S")
        ttft = f"{t['ttft_ms'] / 1000:.1f}s" if t["ttft_ms"] else "-"
        # 端到端列与页脚同门槛：wait≥5s 才用剔等待分母且必带剔等待列标注
        wait_ms = t.get("wait_ms") or 0
        active = t.get("tps_e2e_active")
        e2e = active if (active and wait_ms >= 5000) else t.get("tps_e2e")
        wait = fmt_dur(wait_ms / 1000) if (active and wait_ms >= 5000) else "-"
        dec = t.get("tps")
        print(f"{ts:<20}{fmt_dur((t['run_ms'] or 0) / 1000):>9}{ttft:>9}"
              f"{(fmt_rate(e2e) if e2e else '-'):>14}{wait:>8}{(fmt_rate(dec) if dec else '-'):>9}{fmt_tok(t['out_tokens']):>8}"
              f"  {t['status']}/{'/'.join(t['models']) or '-'}")
    return 0


# ---------------------------------------------------------------- 入口

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    mode = argv[1]
    if mode == "hook":
        sub = argv[2] if len(argv) > 2 else ""
        if sub == "reset":
            mode_hook(sub)
            return 0
        # 旧 post/stop 注入路径已按「零上下文注入」承诺移除：未知参数直接退出
        print(f"hook 子命令 {sub!r} 已下线（仅保留 reset）", file=sys.stderr)
        return 1
    if mode == "session_start":
        mode_session_start()
        return 0
    if mode == "footer":
        limit = 20
        if "--limit" in argv:
            i = argv.index("--limit")
            if i + 1 < len(argv):
                limit = int(argv[i + 1])
        return mode_footer(limit)
    if mode == "ui-install":
        port, skip_verify = DEFAULT_PORT, False
        args = argv[2:]
        for i, a in enumerate(args):
            if a == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
            if a == "--skip-verify":
                skip_verify = True
        return ui_install(port, skip_verify)
    if mode == "ui-uninstall":
        return ui_uninstall()
    if mode == "ui-status":
        return ui_status()
    if mode == "live":
        return mode_live()
    if mode == "line":
        sid = argv[2] if len(argv) > 2 else "auto"
        return mode_line(sid)
    if mode == "report":
        sid, n_turns, n_calls = "auto", 8, 10
        args = argv[2:]
        for i, a in enumerate(args):
            if a == "--session" and i + 1 < len(args):
                sid = args[i + 1]
            if a == "--turns" and i + 1 < len(args):
                n_turns = int(args[i + 1])
            if a == "--calls" and i + 1 < len(args):
                n_calls = int(args[i + 1])
        return mode_report(sid, n_turns, n_calls)
    if mode == "serve":
        port, sid = 7864, "auto"
        args = argv[2:]
        for i, a in enumerate(args):
            if a == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
            if a == "--session" and i + 1 < len(args):
                sid = args[i + 1]
        mode_serve(port, sid)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
