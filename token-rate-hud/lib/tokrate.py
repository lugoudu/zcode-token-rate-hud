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
  tokrate.py hook post    供 PostToolUse hook 调用：读 stdin，注入实时速率行
  tokrate.py hook stop    供 Stop hook 调用：注入本轮总结
  tokrate.py hook reset   供 SessionStart hook 调用：轻量重置节流状态
  tokrate.py session_start 供 SessionStart hook 调用：重置 + 按需拉起界面数据服务
  tokrate.py report [--session SID | auto] [--turns N] [--calls N]
  tokrate.py serve  [--port 7864] [--session SID | auto]
  tokrate.py footer [--limit N]    打印界面页脚将要展示的轮数据（调试）
  tokrate.py ui-install [--port N] [--no-ctx]
  tokrate.py ui-status
  tokrate.py ui-uninstall
  tokrate.py line   [--session SID | auto]   # 只打印将要注入的实时行（调试）

界面页脚（可选 UI 模块）：把统计行画在每条回答下方（DeepSeek 风格）。ZCode 渲染层
没有插件 UI 接口，该模块通过给 app.asar 注入一行脚本实现：ui-install 自动备份原包、
注入、重打包、校验；ui-uninstall 还原；ZCode 升级覆盖后重跑 ui-install 即可。

环境变量配置：
  TOKEN_RATE_INTERVAL  实时行最小注入间隔秒数（默认 8；0 = 每次工具调用后都注入）
  TOKEN_RATE_MAX_CTX   上下文窗口上限 token 数，用于占用百分比（默认 0 = 不显示百分比；
                       例如 GLM-5.x 设 128000、Claude 系设 200000）
  TOKEN_RATE_KEEP      状态中保留的最近调用条数（默认 128）
  TOKEN_RATE_IDLE_EXIT 服务空闲多少秒后自动退出（默认 21600；launchd 常驻进程设为
                       999999999 即不退出，由 launchd KeepAlive 保活）

口径说明：部分通道（GLM 编程套餐）的 inputTokens 已包含 cacheReadTokens，
部分通道（Anthropic 风格）则不包含。本工具按 “inputTokens >= cacheReadTokens
则视为包含” 自适应，入速率与 ctx 估算均使用去重后的有效入 token。
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
HUD_OFF_FLAG = os.path.join(STATE_DIR, "hud-off")  # 存在则不注入任务窗口 HUD 行
STATE_TTL = 14 * 86400  # 状态文件保留天数

try:
    MAX_CTX = int(os.environ.get("TOKEN_RATE_MAX_CTX") or 0)
except ValueError:
    MAX_CTX = 0  # 0 = 不显示占用百分比（各模型上限不同，避免误导）
MIN_INTERVAL = float(os.environ.get("TOKEN_RATE_INTERVAL") or 8)
KEEP_CALLS = int(os.environ.get("TOKEN_RATE_KEEP") or 128)
ROLLING_WINDOW = 90.0  # 滚动窗口秒数
NO_REPLY_TAG = "（token 遥测，无需回应）"

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


def load_calls(session_id, path, keep=KEEP_CALLS):
    """
    增量读取 rollout 文件。状态文件缓存已消费的字节偏移、累计总量与最近 N 条调用。
    返回 (calls, totals, state)，并发安全（文件锁 + 原子替换）。
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    sp, lp = state_paths(session_id)
    state = {}
    try:
        with open(sp, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict) or state.get("v") != 2:
            state = {}
    except (OSError, ValueError):
        state = {}

    calls = state.get("calls") or []
    totals = state.get("totals") or {"n": 0, "out": 0, "in": 0, "cr": 0, "cw": 0, "dur": 0.0}
    offset = int(state.get("offset") or 0)

    try:
        size = os.path.getsize(path)
    except OSError:
        return calls, totals, state

    if offset > size:  # 文件被截断/轮转，从头重建
        offset, calls, totals = 0, [], {"n": 0, "out": 0, "in": 0, "cr": 0, "cw": 0, "dur": 0.0}

    if offset < size:
        appended_calls = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue  # 快速跳过无 usage 的失败/心跳行（可能携带超大请求体）
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                c = parse_call(obj)
                if c:
                    appended_calls.append(c)
            offset = f.tell()
        if appended_calls:
            for c in appended_calls:
                totals["n"] += 1
                totals["out"] += c["out"]
                totals["in"] += c["in_eff"]
                totals["cr"] += c["cr"]
                totals["cw"] += c["cw"]
                totals["dur"] += c["dur"]
            calls = (calls + appended_calls)[-keep:]
        state["v"] = 2
        state["offset"] = offset
        state["calls"] = calls
        state["totals"] = totals
        tmp = sp + ".tmp"
        with open(lp, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False)
                os.replace(tmp, sp)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    return calls, totals, state


# ---------------------------------------------------------------- 统计计算

def stats_for(calls, totals, session_id):
    """汇总当前调用、本轮、滚动窗口、会话累计与上下文占用。"""
    if not calls:
        return None
    last = calls[-1]
    cur_turn = last["turn"]

    turn_calls = [c for c in calls if c["turn"] == cur_turn]
    turn_span = max(
        (max(c["t1"] for c in turn_calls) - min(c["t0"] for c in turn_calls)) if turn_calls else 0.0, 0.0
    )
    turn_dur_sum = sum(c["dur"] for c in turn_calls)
    turn_out = sum(c["out"] for c in turn_calls)
    turn_in = sum(c.get("in_eff", c["in"] + c["cr"]) for c in turn_calls)

    now = last["t1"]
    win = [c for c in calls if now - c["t1"] <= ROLLING_WINDOW]
    win_out = sum(c["out"] for c in win)
    win_dur = sum(c["dur"] for c in win) or 1e-9

    rates = [c["out"] / c["dur"] for c in turn_calls if c["dur"] > 0.2 and c["out"] > 0]

    ctx_tokens = last.get("in_eff", last["in"] + last["cr"]) + last["out"]
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
            "id": cur_turn, "n": len(turn_calls), "span": turn_span,
            "out": turn_out, "in": turn_in,
            "avg": (turn_out / turn_dur_sum) if turn_dur_sum > 0 else 0.0,
            "peak": max(rates) if rates else 0.0,
        },
        "rolling": {"rate": win_out / win_dur, "out": win_out, "n": len(win)},
        "totals": {
            "n": totals["n"], "out": totals["out"],
            "in": totals["in"] + totals["cr"], "cr": totals["cr"],
            "gen": totals["dur"],
            "avg": totals["out"] / total_gen,
        },
        "ctx": {"tokens": ctx_tokens, "max": MAX_CTX,
                "pct": (round(ctx_tokens * 100.0 / MAX_CTX) if MAX_CTX else None)},
    }


# ---------------------------------------------------------------- 注入文案

def ctx_part(s):
    c = s["ctx"]
    if c["pct"] is not None:
        return f"ctx {fmt_tok(c['tokens'])}/{fmt_tok(c['max'])}（{c['pct']}%）"
    return f"ctx {fmt_tok(c['tokens'])}"


def line_post(s):
    cur, turn = s["current"], s["turn"]
    return (
        f"{MARK_OK}｜出 {fmt_rate(cur['rate_out'])} tok/s · "
        f"入 {fmt_rate(cur['rate_in'])} tok/s · 缓存读 {fmt_tok(cur['cr'])}｜"
        f"本轮 {turn['n']} 次 · 出 {fmt_tok(turn['out'])}（均 {fmt_rate(turn['avg'])}）｜"
        f"{ctx_part(s)}{NO_REPLY_TAG}"
    )


def line_stop(s):
    turn, tot = s["turn"], s["totals"]
    return (
        f"🏁{MARK_OK} 本轮：{fmt_dur(turn['span'])} · {turn['n']} 次调用 · "
        f"出 {fmt_tok(turn['out'])} tok（均 {fmt_rate(turn['avg'])} · 峰 {fmt_rate(turn['peak'])} tok/s）· "
        f"入 {fmt_tok(turn['in'])}｜"
        f"会话累计 {tot['n']} 次 · 出 {fmt_tok(tot['out'])} · 入 {fmt_tok(tot['in'])} · "
        f"纯生成 {fmt_dur(tot['gen'])}｜{ctx_part(s)}{NO_REPLY_TAG}"
    )


# ---------------------------------------------------------------- CLI 模式

def read_hook_stdin():
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        obj = json.loads(raw) if raw.strip() else {}
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def emit_context(event, text):
    """以引擎认可的严格 schema 输出 additionalContext。

    只放 hookSpecificOutput 一处：引擎对顶层与 hookSpecificOutput 的
    additionalContext 会分别收集，两处都写会导致重复注入。"""
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


def mode_hook(sub):
    obj = read_hook_stdin()
    sid = obj.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or \
        os.environ.get("ZCODE_SESSION_ID") or ""

    if sub == "reset":
        # 轻量重置：清空节流状态、清理过期状态文件；保留增量偏移与累计值
        sp, _ = state_paths(sid)
        try:
            with open(sp, "r", encoding="utf-8") as f:
                state = json.load(f)
            state.pop("last_inject", None)
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except (OSError, ValueError):
            pass
        prune_states(sid)
        return

    # 装了界面页脚后，任务窗口注入行成为可选项（它占用上下文，页脚不占）
    if os.path.exists(HUD_OFF_FLAG):
        return

    sid_resolved, path = resolve_session(sid)
    if not path:
        return  # 找不到数据源：静默退出，绝不干扰会话
    calls, totals, state = load_calls(sid_resolved, path)
    s = stats_for(calls, totals, sid_resolved)
    if not s:
        return

    if sub == "stop":
        emit_context("Stop", line_stop(s))
        return

    # post：节流 + 去重 + 轮切换强制刷新
    li = state.get("last_inject") or {}
    line = line_post(s)
    if li.get("text") == line:
        return
    if MIN_INTERVAL > 0 and li.get("turn") == s["turn"]["id"] and \
            time.time() - float(li.get("ts") or 0) < MIN_INTERVAL:
        return
    sp, lp = state_paths(sid_resolved)
    try:
        with open(lp, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                cur = {}
                try:
                    with open(sp, "r", encoding="utf-8") as f:
                        cur = json.load(f)
                except (OSError, ValueError):
                    pass
                cur["last_inject"] = {"ts": time.time(), "text": line, "turn": s["turn"]["id"]}
                tmp = sp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cur, f, ensure_ascii=False)
                os.replace(tmp, sp)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    emit_context("PostToolUse", line)


def mode_session_start():
    """SessionStart hook：重置节流状态；若界面页脚启用，按需拉起本地数据服务。

    注意：本模式由 hook 调用，stdout 必须保持为空或合法 JSON，所有提示写入日志。"""
    mode_hook("reset")
    if os.path.exists(ENABLED_FLAG):
        msg = _ensure_server(_server_port(), quiet=True)
        ui_log(f"[session_start] {msg}")


def mode_live():
    """调试：打印当前进行中轮次的实时行（与注入脚本拿到的 /live 一致）。"""
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
    if lv["ctx_tokens"]:
        parts.append(f"ctx {fmt_tok(lv['ctx_tokens'])}")
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
                  f"{a['n_calls']:>3} 次 {fmt_tok(a['out_tokens'])} tok")
    return 0


def mode_line(session_id):
    sid, path = resolve_session(session_id)
    if not path:
        print("（未找到 rollout 数据源）")
        return 1
    calls, totals, _ = load_calls(sid, path)
    s = stats_for(calls, totals, sid)
    print(line_post(s) if s else "（暂无 usage 数据）")
    return 0


def mode_report(session_id, n_turns, n_calls):
    sid, path = resolve_session(session_id)
    if not path:
        print("未找到 model-io 数据源（需要 ZCode ≥ rollout 日志版本）")
        return 1
    calls, totals, _ = load_calls(sid, path)
    if not calls:
        print(f"会话 {sid} 暂无 usage 记录")
        return 0
    s = stats_for(calls, totals, sid)

    print(f"⚡ token-rate 会话报告  {sid}")
    print(f"模型 {s['model']} · 共 {totals['n']} 次模型调用\n")

    print(f"最近调用（新→旧，至多 {n_calls} 条）")
    print(f"  {'时间':<9}{'时长':>7}{'出tok':>8}{'出速率':>10}{'入tok':>9}{'缓存读':>9}")
    for c in reversed(calls[-n_calls:]):
        rate = (c["out"] / c["dur"]) if c["dur"] > 0 else 0.0
        print(f"  {fmt_clock(c['t1']):<9}{fmt_dur(c['dur']):>7}{c['out']:>8}"
              f"{fmt_rate(rate) + '/s':>10}{c['in']:>9}{fmt_tok(c['cr']):>9}")

    turns = []
    seen = {}
    for c in calls:
        seen.setdefault(c["turn"], []).append(c)
    for tid, cs in seen.items():
        turns.append((max(x["t1"] for x in cs), tid, cs))
    turns.sort(reverse=True)
    print(f"\n按轮汇总（本状态窗口内，至多 {n_turns} 轮）")
    print(f"  {'轮次':<20}{'调用':>5}{'墙钟':>9}{'出tok':>9}{'均出速率':>10}{'入tok':>10}")
    for _, tid, cs in turns[:n_turns]:
        span = max(x["t1"] for x in cs) - min(x["t0"] for x in cs)
        dur_sum = sum(x["dur"] for x in cs) or 1e-9
        out = sum(x["out"] for x in cs)
        tin = sum(x.get("in_eff", x["in"] + x["cr"]) for x in cs)
        print(f"  {tid[:18]:<20}{len(cs):>5}{fmt_dur(span):>9}{fmt_tok(out):>9}"
              f"{fmt_rate(out / dur_sum) + '/s':>10}{fmt_tok(tin):>10}")

    t = s["totals"]
    print(f"\n会话累计：出 {fmt_tok(t['out'])} · 入 {fmt_tok(t['in'])}（缓存读 {fmt_tok(t['cr'])}）· "
          f"纯生成 {fmt_dur(t['gen'])} · 整体出速率 {fmt_rate(t['avg'])} tok/s")
    print(f"上下文占用：约 {fmt_tok(s['ctx']['tokens'])}" +
          (f" / {fmt_tok(s['ctx']['max'])}（{s['ctx']['pct']}%）" if s['ctx']['pct'] is not None else ""))
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
<div class="big"><span id="rate">--</span><span class="unit">tok/s 出</span></div>
<div class="row">
 <div class="card"><div class="k">入速率（当前调用）</div><div class="v" id="rin">--</div></div>
 <div class="card"><div class="k">本轮均值 / 峰值</div><div class="v" id="turn">--</div></div>
 <div class="card"><div class="k">会话累计出 / 入</div><div class="v" id="tot">--</div></div>
 <div class="card"><div class="k">上下文占用</div><div class="v" id="ctx">--</div></div>
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
   +(w.tps?' · 累计 '+r(w.tps)+' tok/s':'')+' · '+f(w.out_tokens)+' tok</span></div>';
  h+=(w.per_actor||[]).map(a=>'<div class="wfrow"><span style="width:1em">'
   +(a.active?'●':'·')+'</span><span class="nm">'+esc(a.name)+'</span><span>'
   +(a.instant_tps?('~'+r(a.instant_tps)+' tok/s')
      :(a.active?(a.tps?(r(a.tps)+' tok/s'):'生成中…')
        :(a.n_calls?('✓ '+f(a.out_tokens)+' tok'):'等待中')))
   +'</span><span class="dim">'+a.n_calls+' 次 · '+f(a.out_tokens)+' tok'
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
  ctx.textContent=s.ctx?(f(s.ctx.tokens)+(s.ctx.pct!=null?'（'+s.ctx.pct+'%）':'')):'--';
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
    live_cache = {}   # scope 会话 id -> {"at", "data"}（每窗口一个实时轮缓存）
    activity = {"at": time.time()}

    # 流式瞬时速率：轮询最新流式部件的字符增量换算 tok/s；
    # chars/token 比随调用完成自动校准（EMA），持久化在 ui/calib.json
    calib_path = os.path.join(UI_DIR, "calib.json")
    try:
        _c = json.load(open(calib_path))
        _calib = float(_c.get("chars_per_token") or 0)
    except (OSError, ValueError, TypeError):
        _calib = 0.0
    if not 0.8 <= _calib <= 6.0:
        _calib = 2.4  # 中英混排默认值，几步调用后即被实测校准
    stream = {
        "pid": None, "chars": 0, "ts": 0.0, "tps": None, "last_grow": 0.0,
        "calib": _calib, "last_out": -1, "last_chars_total": -1,
    }

    # 工作流子代理实时块：workflow_live() 按主会话聚合 + 逐个活跃子会话的流式
    # 字符增量估算瞬时速率（与主 stream 共用同一 chars/token 校准比）。
    # scope 未解析（渲染层没报 cid）时不返回工作流数据——宁可少显示也不串台。
    wf_cache = {}     # scope 会话 id -> {"at", "data"}
    wf_stream = {}

    def _wf_payload(scope):
        c = wf_cache.setdefault(scope, {"at": 0.0, "data": []})
        if len(wf_cache) > 8:  # 窗口数有限，防御性清理
            for k in sorted(wf_cache, key=lambda k: wf_cache[k]["at"])[:-4]:
                wf_cache.pop(k, None)
        if time.time() - c["at"] > 1.0:
            data = []
            if scope:
                try:
                    data = usage_db.workflow_live(parent_session_id=scope) or []
                except Exception:
                    data = []
            now = time.time()
            seen = set()
            for run in data:
                agg = 0.0
                for a in run.get("per_actor", []):
                    sid_a = a.get("session_id")
                    if not sid_a:
                        continue
                    seen.add(sid_a)
                    st = wf_stream.setdefault(
                        sid_a, {"pid": None, "chars": 0, "ts": 0.0,
                                "tps": None, "last_grow": 0.0}
                    )
                    try:
                        sc = usage_db.stream_chars(sid_a)
                    except Exception:
                        sc = None
                    if sc:
                        if sc[0] == st["pid"]:
                            if sc[1] > st["chars"] and now - st["ts"] >= 0.5:
                                cps = (sc[1] - st["chars"]) / (now - st["ts"])
                                st["tps"] = round(cps / stream["calib"], 1)
                                st["last_grow"] = now
                        else:
                            st["pid"], st["chars"], st["ts"] = sc[0], sc[1], now
                        if sc[0] == st["pid"]:
                            st["chars"], st["ts"] = sc[1], now
                    if now - st["last_grow"] > 3.0:
                        st["tps"] = None  # 该子代理超 3 秒无字符增长：瞬时作废
                    a["instant_tps"] = st["tps"]
                    if a.get("active") and st["tps"]:
                        agg += st["tps"]
                run["agg_instant"] = round(agg, 1) if agg else None
            for sid_a in [k for k in wf_stream if k not in seen]:
                wf_stream.pop(sid_a, None)  # 运行结束/宽限过期：清理跟踪器
            c["data"] = data
            c["at"] = now
        return c["data"]

    # 渲染层报来的 cid（本窗口 DOM 的 data-turn-id）→ 会话 id，小缓存
    cid_cache = {"at": 0.0, "map": {}}

    def _scope_of_cid(cid):
        if not cid:
            return None
        now = time.time()
        if now - cid_cache["at"] > 300:
            cid_cache["map"].clear()
            cid_cache["at"] = now
        if cid not in cid_cache["map"]:
            cid_cache["map"][cid] = usage_db.session_of_message(cid)
        return cid_cache["map"][cid]

    def _live_payload(scope):
        lv = usage_db.live_turn(session_id=scope) if scope else usage_db.live_turn()
        now = time.time()
        st = stream
        if lv and lv.get("session_id"):
            try:
                sc = usage_db.stream_chars(lv["session_id"])
            except Exception:
                sc = None
            if sc:
                if sc[0] == st["pid"]:
                    if sc[1] > st["chars"] and now - st["ts"] >= 0.5:
                        cps = (sc[1] - st["chars"]) / (now - st["ts"])
                        st["tps"] = round(cps / st["calib"], 1)
                        st["last_grow"] = now
                else:
                    st["pid"], st["chars"], st["ts"] = sc[0], sc[1], now
                if sc[0] == st["pid"]:
                    st["chars"], st["ts"] = sc[1], now
            if now - st["last_grow"] > 3.0:
                st["tps"] = None  # 超过 3 秒无字符增长（工具执行/等待）：瞬时速率作废
            if lv.get("phase") == 1:
                # 校准：调用完成边界（累计输出 token 增长）用字符增量修正 chars/token 比
                if st["last_out"] >= 0 and lv["out_tokens"] > st["last_out"]:
                    try:
                        tot = usage_db.stream_chars_total(lv["session_id"])
                    except Exception:
                        tot = 0
                    if st["last_chars_total"] >= 0 and tot > st["last_chars_total"] \
                            and lv["out_tokens"] - st["last_out"] >= 50:
                        ratio = (tot - st["last_chars_total"]) / (lv["out_tokens"] - st["last_out"])
                        if 0.8 <= ratio <= 6.0:
                            st["calib"] = round(st["calib"] * 0.7 + ratio * 0.3, 3)
                            try:
                                json.dump({"chars_per_token": st["calib"]}, open(calib_path, "w"))
                            except OSError:
                                pass
                    st["last_chars_total"] = tot
                st["last_out"] = lv["out_tokens"]
        else:
            st["tps"], st["pid"] = None, None
        if lv:
            lv["instant_tps"] = st["tps"]
            lv["calib"] = st["calib"]  # 渲染层字符→token 换算比（自动校准）
        return lv

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
            activity["at"] = time.time()
            try:
                if self.path.startswith("/turns"):
                    limit = 800
                    if "limit=" in self.path:
                        try:
                            limit = min(int(self.path.split("limit=")[1].split("&")[0]), 2000)
                        except ValueError:
                            pass
                    turns = turn_cache.get()[:limit]
                    return self._send(json.dumps({"turns": turns}, ensure_ascii=False))
                if self.path.startswith("/live"):
                    # 进行中轮次的实时数据（每会话 1s 缓存；注入脚本每秒轮询）。
                    # cid = 渲染层上报的本窗口 data-turn-id（user 消息 id），服务端
                    # 解析出会话后整套数据（实时行 + workflow 块）都限定在该会话内，
                    # 多窗口/自动化并存时互不串显；cid 解析失败则不给工作流数据。
                    from urllib.parse import unquote
                    cid = ""
                    if "cid=" in self.path:
                        try:
                            cid = unquote(self.path.split("cid=", 1)[1].split("&")[0])
                        except ValueError:
                            cid = ""
                    scope = _scope_of_cid(cid)
                    c = live_cache.setdefault(scope, {"at": 0.0, "data": None})
                    if len(live_cache) > 8:
                        for k in sorted(live_cache, key=lambda k: live_cache[k]["at"])[:-4]:
                            live_cache.pop(k, None)
                    if time.time() - c["at"] > 1.0:
                        try:
                            c["data"] = _live_payload(scope)
                        except Exception:
                            pass  # 查询失败沿用上次结果
                        c["at"] = time.time()
                    return self._send(json.dumps(
                        {"live": c["data"], "workflow": _wf_payload(scope)},
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
                        calls, totals, _ = load_calls(cur_sid, p)
                        s = stats_for(calls, totals, cur_sid)
                        if s:
                            s["calls"] = [
                                {"t": c["t1"], "dur": round(c["dur"], 1), "out": c["out"],
                                 "rate": round(c["out"] / c["dur"], 1) if c["dur"] > 0 else 0}
                                for c in calls[-64:]
                            ]
                            body = s
                    # 仪表盘工作流面板：同样限定在仪表盘所跟踪的会话内
                    body["workflow"] = _wf_payload(cur_sid)
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
            if time.time() - activity["at"] > IDLE_EXIT_SECONDS:
                ui_log(f"[serve] 空闲超过 {IDLE_EXIT_SECONDS:.0f}s，自动退出")
                srv.shutdown()
                return

    threading.Thread(target=idle_watch, daemon=True).start()
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


def _write_stage_js(port, show_ctx):
    os.makedirs(UI_DIR, exist_ok=True)
    with open(INJECT_SRC, encoding="utf-8") as f:
        src = f.read()
    src = src.replace("__PORT__", str(port)).replace("__SHOW_CTX__", "true" if show_ctx else "false")
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


def ui_install(port, show_ctx, skip_verify=False):
    if not os.path.exists(ASAR):
        print(f"✗ 找不到 {ASAR}，界面页脚模块仅支持 macOS 桌面版 ZCode")
        return 1
    if _quarantined():
        print("⚠️  ZCode.app 带 quarantine 隔离标记：改包后 Gatekeeper 可能阻止启动。")
        print("    如遇无法启动，先执行 ui-uninstall 还原；仍要尝试可先去掉隔离标记。")

    _write_stage_js(port, show_ctx)
    with open(PORT_FILE, "w") as f:
        f.write(str(port))
    if not os.path.exists(ENABLED_FLAG):
        open(ENABLED_FLAG, "w").close()
    print(f"① 界面脚本已就位：{STAGE_JS}（端口 {port}，ctx 显示 {'开' if show_ctx else '关'}）")

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
    print(f"{'完成时间':<20}{'用时':>9}{'首token':>9}{'tok/s':>8}{'出tok':>8}{'ctx':>8}  状态/模型")
    for t in turns:
        ts = datetime.fromtimestamp((t["end_ms"] or 0) / 1000).strftime("%m-%d %H:%M:%S")
        ttft = f"{t['ttft_ms'] / 1000:.1f}s" if t["ttft_ms"] else "-"
        print(f"{ts:<20}{fmt_dur((t['run_ms'] or 0) / 1000):>9}{ttft:>9}"
              f"{(fmt_rate(t['tps']) if t['tps'] else '-'):>8}{fmt_tok(t['out_tokens']):>8}"
              f"{(fmt_tok(t['ctx_tokens']) if t['ctx_tokens'] else '-'):>8}  "
              f"{t['status']}/{'/'.join(t['models']) or '-'}")
    return 0


# ---------------------------------------------------------------- 入口

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    mode = argv[1]
    if mode == "hook":
        return (mode_hook(argv[2] if len(argv) > 2 else "post") or 0)
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
        port, show_ctx, skip_verify = DEFAULT_PORT, True, False
        args = argv[2:]
        for i, a in enumerate(args):
            if a == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
            if a == "--no-ctx":
                show_ctx = False
            if a == "--skip-verify":
                skip_verify = True
        return ui_install(port, show_ctx, skip_verify)
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
