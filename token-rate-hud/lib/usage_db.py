#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usage_db.py —— ZCode 本地用量库（SQLite）只读折叠层

数据源：~/.zcode/cli/db/db.sqlite（ZCode CLI 官方用量库，只读打开）
  - turn_usage  ：按轮聚合（含 user_message_id，作为界面 section[data-turn-id] 的桥）
  - model_usage ：按次调用明细（含 time_to_first_token_ms，用于剔除首包等待的纯解码速率）

口径（页脚主展示为端到端速率；tps 仅作参考值随 /turns 下发、界面不展示）：
  - run_ms  = MAX(completed_at) - MIN(started_at)   整轮墙钟，含工具执行时段
  - ttft_ms = 本轮最早发起那一步的首 token 延迟
  - tps_e2e = turn_usage 整轮输出 ÷ run_ms  端到端速率（含首包等待与工具执行时段）
  - wait_ms = 轮内时间线上工具开始执行前的空闲段合计（单段 ≥ WAIT_GAP_MIN_MS 才计，
              滤除毫秒级调度噪声）。数据源 tool_usage.started_at 是工具实际开跑时刻
              （用户确认之后），空档即等待：主要为权限确认等待，也含后台任务轮询
              等其他空闲；AskUserQuestion 的用户思考时间在工具时长内，不在其中
  - tps_e2e_active = 整轮输出 ÷ (run_ms - wait_ms)  剔等待端到端（页脚优先展示）
  - tps     = 有效调用 Σoutput ÷ Σ(duration_ms - ttft_ms)  首输出后调用均值（参考值）。
              有效性判据分子分母同条件：ttft 非空、duration>ttft、输出>0，
              无效样本两侧同剔，避免只进分子抬高均值
  - models  = 本轮用过的模型（去重，斜杠拼接用）
  - ctx     = 本轮最后一次 main_turn 调用的上下文占用近似值

过滤：model_usage 仅取 status='completed' 且 query_source='main_turn'，
      排除标题生成、压缩等旁路调用与失败重试。

工作流（2026 新功能）：子代理调用 query_source='workflow_child'，落在独立
子会话（sess_dwf-dwfrun-…-actor_N_M）。关联链 dwf_run(parent_session_id)
→ dwf_actor(名字/子会话) → model_usage；fold_turns 把其用量按时间窗归并进
主会话对应轮次的 wf_* 字段（不并入主代理 tps，避免并行失真），
workflow_live() 供 /live 实时块按子代理聚合。普通 Agent 工具子代理
（query_source='subagent'）暂未纳入，仅做了会话排除防劫持。
"""

import sqlite3
import time

DB_PATH = "~/.zcode/cli/db/db.sqlite"

# 工具开始执行前的空闲段达到该值才计入 wait_ms（毫秒级调度噪声 5~30ms，放行即跑）
WAIT_GAP_MIN_MS = 2000


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)


def db_available(path=DB_PATH):
    import os
    return os.path.exists(os.path.expanduser(path))


def fold_turns(path=DB_PATH, limit=800):
    """
    返回按 end_ms 降序的轮列表：
      {turn_id, msg_id, session_id, status, start_ms, end_ms, run_ms,
       ttft_ms, wait_ms, tps_e2e, tps_e2e_active, tps, out_tokens, models, ctx_tokens}
    """
    import os
    path = os.path.expanduser(path)
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT session_id, turn_id, user_message_id, status,
                   started_at, completed_at, time_to_first_token_ms, output_tokens
            FROM turn_usage
            WHERE user_message_id IS NOT NULL AND user_message_id != ''
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if not rows:
            return []
        turn_ids = [r[1] for r in rows]

        # 按轮折叠解码时间、模型列表，并抓每轮最后一次调用的上下文占用
        # calls = 该轮已完成的主对话调用数（与实时行 n_calls 同口径）。
        # 按轮次 ID 分块取全调用明细——不做时间窗行数上限，杜绝大库下
        # 较新调用被静默截断（模型列表/ctx/解码参考值会随之失真）。
        # 解码有效性分子分母同条件，无效调用两侧同剔。
        # busy = 轮内忙碌时间线（模型调用 + 工具执行区间），用于剥离
        # 工具开始前的等待空档（权限确认等待等）。
        _VALID = """time_to_first_token_ms IS NOT NULL
                    AND duration_ms IS NOT NULL
                    AND duration_ms - time_to_first_token_ms > 0
                    AND output_tokens > 0"""
        dec = {}
        calls_by_turn = {}
        models_by_turn = {}
        ctx_by_turn = {}
        busy = {}  # turn_id -> [(start_ms, end_ms, is_tool), ...]
        for i in range(0, len(turn_ids), 400):
            chunk = turn_ids[i:i + 400]
            qm = ",".join("?" * len(chunk))
            for turn_id, model_id, started, decode_ms, decode_tok, ctx in conn.execute(
                f"""
                SELECT turn_id, model_id, started_at,
                       CASE WHEN {_VALID}
                            THEN duration_ms - time_to_first_token_ms ELSE 0 END,
                       CASE WHEN {_VALID} THEN output_tokens ELSE 0 END,
                       computed_total_tokens
                    FROM model_usage
                    WHERE turn_id IN ({qm})
                      AND status = 'completed' AND query_source = 'main_turn'
                    ORDER BY started_at ASC
                    """,
                chunk,
            ):
                d, tok = dec.get(turn_id, (0, 0))
                dec[turn_id] = (d + (decode_ms or 0), tok + (decode_tok or 0))
                calls_by_turn[turn_id] = calls_by_turn.get(turn_id, 0) + 1
                if model_id:
                    lst = models_by_turn.setdefault(turn_id, [])
                    if model_id not in lst:
                        lst.append(model_id)
                if ctx is not None:
                    ctx_by_turn[turn_id] = ctx  # 后写覆盖，最终为本轮最后一次
            # 忙碌时间线：全部已完成调用（不限 main_turn——子代理/旁路调用同样
            # 占用机器，不能被当等待扣除）+ 工具执行区间。工具的 started_at 是
            # 实际开跑时刻（用户确认之后），它与上一活动结束之间的空档即等待。
            # completed_at 为空的（罕见）用 duration_ms 兜底。
            for turn_id, started, call_end in conn.execute(
                f"""
                SELECT turn_id, started_at, completed_at FROM model_usage
                WHERE turn_id IN ({qm}) AND status = 'completed'
                """,
                chunk,
            ):
                busy.setdefault(turn_id, []).append((started, call_end or started, False))
            for turn_id, t_start, t_end in conn.execute(
                f"""
                SELECT turn_id, started_at,
                       COALESCE(completed_at, started_at + duration_ms, started_at)
                    FROM tool_usage
                    WHERE turn_id IN ({qm}) AND started_at IS NOT NULL
                """,
                chunk,
            ):
                busy.setdefault(turn_id, []).append((t_start, t_end, True))

        def _wait_before_tools(intervals, min_gap_ms=WAIT_GAP_MIN_MS):
            """合并时间线上，由工具开启的空闲段合计（并行工具贴着前一个跑，不重复计）。"""
            wait = 0
            prev_end = None
            for s, e, is_tool in sorted(intervals, key=lambda x: x[0]):
                e = max(e, s)
                if prev_end is not None and is_tool and s - prev_end >= min_gap_ms:
                    wait += s - prev_end
                prev_end = e if prev_end is None else max(prev_end, e)
            return wait

        wait_by_turn = {tid: _wait_before_tools(iv) for tid, iv in busy.items()}

        # 工作流子代理归并备料：dwf_run → dwf_actor → model_usage(workflow_child)
        # 两次批量查询拿到「哪个主会话的哪段时间窗里跑了哪些工作流用量」，
        # 旧版 ZCode 无 dwf_* 表时整段跳过（静默，不影响主口径）。
        min_start = min(r[4] or 0 for r in rows)
        wf_runs = []
        wf_agg = {}
        try:
            wf_runs = conn.execute(
                "SELECT id, parent_session_id, time_created FROM dwf_run "
                "WHERE time_created >= ?",
                (min_start,),
            ).fetchall()
            if wf_runs:
                qm = ",".join("?" * len(wf_runs))
                for rid, calls, out_tok, total_tok, actors in conn.execute(
                    f"""
                    SELECT a.run_id, COUNT(m.id),
                           COALESCE(SUM(m.output_tokens), 0),
                           COALESCE(SUM(CASE WHEN m.computed_total_tokens IS NOT NULL
                                         THEN m.computed_total_tokens
                                         ELSE m.input_tokens + m.output_tokens END), 0),
                           COUNT(DISTINCT CASE WHEN m.id IS NOT NULL THEN a.session_id END)
                    FROM dwf_actor a
                    LEFT JOIN model_usage m
                           ON m.session_id = a.session_id
                          AND m.query_source = 'workflow_child'
                          AND m.status = 'completed'
                    WHERE a.run_id IN ({qm})
                    GROUP BY a.run_id
                    """,
                    [r[0] for r in wf_runs],
                ):
                    wf_agg[rid] = (calls, out_tok, total_tok, actors)
        except sqlite3.Error:
            wf_runs = []
    finally:
        conn.close()

    out = []
    for sid, tid, msg_id, status, started, completed, ttft, out_tok in rows:
        completed = completed or started
        decode_ms, decode_tok = dec.get(tid, (0, 0))
        tps = (decode_tok * 1000.0 / decode_ms) if decode_ms > 0 else None
        run_ms = max(0, (completed or 0) - (started or 0))
        # 端到端分子用整轮总输出（turn_usage 口径）：ttft 为 NULL 的步算不出解码
        # 速率，但其输出确属整轮产出，墙钟也覆盖它们，不能只计 ttft 齐备的步
        shown_out = out_tok or decode_tok or 0
        tps_e2e = (shown_out * 1000.0 / run_ms) if run_ms > 0 else None
        wait_ms = min(wait_by_turn.get(tid, 0), run_ms) if run_ms > 0 else 0  # 防御：不超过墙钟
        active_ms = run_ms - wait_ms
        # 剔等待值仅对「有主代理完成调用」的轮产出：纯工作流/子代理轮的
        # 输出与净忙时不属于同一主体，剥出的速率无意义
        _has_main = calls_by_turn.get(tid, 0) > 0
        tps_e2e_active = (shown_out * 1000.0 / active_ms) \
            if (_has_main and wait_ms > 0 and active_ms > 0) else None
        out.append(
            {
                "turn_id": tid,
                "msg_id": msg_id,
                "session_id": sid,
                "status": status,
                "start_ms": started,
                "end_ms": completed,
                "run_ms": run_ms,
                "ttft_ms": ttft,
                "tps": round(tps, 2) if tps else None,
                "tps_e2e": round(tps_e2e, 2) if tps_e2e else None,
                "wait_ms": wait_ms,
                "tps_e2e_active": round(tps_e2e_active, 2) if tps_e2e_active else None,
                "out_tokens": shown_out,
                "calls": calls_by_turn.get(tid, 0),
                "models": models_by_turn.get(tid, []),
                "ctx_tokens": ctx_by_turn.get(tid),
                "wf_runs": 0,
                "wf_actors": 0,
                "wf_calls": 0,
                "wf_out_tokens": 0,
                "wf_total_tokens": 0,
            }
        )
    # 工作流用量归属：运行创建时刻落在哪个主会话轮次的时间窗内（2s 宽限）。
    # 只并计数字段（wf_*），不并入主代理的 tps/out_tokens——并行执行下合并速率会失真。
    for rid, parent, created in wf_runs:
        if not created:
            continue
        c, o, tot, a = wf_agg.get(rid, (0, 0, 0, 0))
        if not (c or o):
            continue
        for t in out:
            if t["session_id"] == parent and t["start_ms"] - 2000 <= created <= t["end_ms"] + 2000:
                t["wf_runs"] += 1
                t["wf_calls"] += c
                t["wf_out_tokens"] += o
                t["wf_total_tokens"] += tot
                t["wf_actors"] += a
                break
    out.sort(key=lambda t: t["end_ms"] or 0, reverse=True)
    return out


def live_turn(path=DB_PATH, max_age_s=1200, session_id=None):
    """当前进行中的轮：最新 main_turn 调用所属 turn，且尚未落入 turn_usage
    （turn_usage 在轮结束时才写行，进行中的轮只能从 model_usage 聚合）。

    session_id 给定时整条链路（最新调用、最新 user 消息）都限定在该会话内——
    多窗口/自动化并存时，每个窗口只看自己的轮，防止他窗数据串显。

    返回 None 或：
      {turn_id, msg_id, session_id, start_ms, now_ms, elapsed_ms, n_calls,
       ttft_ms, tps, out_tokens, models, ctx_tokens, phase}

    phase=1：本轮已有完成的模型调用（完整统计）；
    phase=0：提问已发出、首次调用尚未完成（只有开始时间，行先亮起来）。
    首次调用进行中（尚无完成行、轮未落 turn_usage）也返回 phase=1 骨架行，
    n_calls=0，由界面显示“首步生成中”。
    msg_id 桥 = 该会话最新一条 user 消息（轮未结束期间它就是本轮提问）。
    """
    import os
    import time as _time

    path = os.path.expanduser(path)
    conn = _connect(path)
    try:
        head = conn.execute(
            """SELECT turn_id, session_id, started_at FROM model_usage
               WHERE query_source = 'main_turn' {sf}
               ORDER BY started_at DESC LIMIT 1""".format(
                sf="AND session_id = ?" if session_id else ""),
            ((session_id,) if session_id else ()),
        ).fetchone()
        now_ms = int(_time.time() * 1000)

        # phase 0 检测：本会话最新一条 user 消息若（a）晚于最近一次 main_turn 调用、
        # （b）尚未被任何 turn_usage 消费、（c）足够新 —— 即为新会话首轮或老会话新一轮
        # 的“提问已发出、首步未完成”窗口，行先亮起来。
        # 必须排除子代理会话（未限定会话时）：工作流/Agent 子代理的会话会持续写入
        # 自己的 user 消息，否则全局最新 user 消息被其劫持，实时行计时错乱（实测发生过）。
        try:
            lu = conn.execute(
                """SELECT id, session_id, time_created FROM message
                   WHERE json_extract(data, '$.role') = 'user' {sf}
                     AND session_id NOT LIKE 'sess_dwf%'
                     AND session_id NOT LIKE 'sess_subagent%'
                   ORDER BY time_created DESC LIMIT 1""".format(
                    sf="AND session_id = ?" if session_id else ""),
                ((session_id,) if session_id else ()),
            ).fetchone()
        except sqlite3.Error:
            lu = None
        if lu and lu[2] and now_ms - lu[2] <= max_age_s * 1000:
            consumed = conn.execute(
                "SELECT 1 FROM turn_usage WHERE user_message_id = ? LIMIT 1", (lu[0],)
            ).fetchone()
            latest_call_started = head[2] if head else 0
            if not consumed and lu[2] > (latest_call_started or 0):
                return {
                    "turn_id": None, "msg_id": lu[0], "session_id": lu[1],
                    "start_ms": lu[2], "now_ms": now_ms,
                    "elapsed_ms": max(0, now_ms - lu[2]), "n_calls": 0,
                    "ttft_ms": None, "tps": None, "out_tokens": 0,
                    "models": [], "ctx_tokens": None, "phase": 0,
                }

        if not head:
            return None
        tid, sid, last_started = head
        # 该主会话是否有运行中的工作流：主代理发起工作流后自身长时间无新调用
        # （原本 20 分钟即超时摘行），且首个 main_turn 完成行可能还没落库——
        # 这两种情形只要工作流在跑，轮次仍视为进行中，工作流块由服务层附上。
        wf_running = _has_running_dwf(conn, sid, now_ms)
        if not last_started or (now_ms - last_started > max_age_s * 1000 and not wf_running):
            return None  # 久无调用落库且无运行中的工作流：不视为进行中
        done = conn.execute(
            "SELECT 1 FROM turn_usage WHERE turn_id = ? LIMIT 1", (tid,)
        ).fetchone()
        if done:
            return None  # 该轮已结束：交给页脚静态行
        rows = conn.execute(
            """SELECT started_at, duration_ms, time_to_first_token_ms,
                      output_tokens, model_id, computed_total_tokens
               FROM model_usage
               WHERE turn_id = ? AND query_source = 'main_turn' AND status = 'completed'
               ORDER BY started_at ASC""",
            (tid,),
        ).fetchall()
        if not rows:
            # 首次调用进行中（尚无完成行）或纯工作流等待：出骨架行，界面显示“首步生成中”。
            # 走到这里时 done 检查已排除已结束的轮，只剩进行中的情形。
            rows = []
        start_ms = (rows[0][0] if rows else None) or last_started
        ttft = rows[0][2] if rows else None
        decode_ms = decode_tok = out_tok = 0
        models = []
        ctx = None
        for _started, dur, ttft_i, out, model, total in rows:
            if ttft_i is not None and dur and (dur - ttft_i) > 0 and out and out > 0:
                decode_ms += dur - ttft_i
                decode_tok += out
            out_tok += out or 0
            if model and model not in models:
                models.append(model)
            if total is not None:
                ctx = total  # 后写覆盖 = 本轮最后一次调用的上下文占用
        tps = (decode_tok * 1000.0 / decode_ms) if decode_ms > 0 else None
        msg_id = None
        try:
            r = conn.execute(
                """SELECT id FROM message
                   WHERE session_id = ? AND json_extract(data, '$.role') = 'user'
                   ORDER BY time_created DESC LIMIT 1""",
                (sid,),
            ).fetchone()
            msg_id = r[0] if r else None
        except sqlite3.Error:
            pass  # role 解析失败：不带桥，注入脚本按运行中节点绑定
        return {
            "turn_id": tid,
            "msg_id": msg_id,
            "session_id": sid,
            "start_ms": start_ms,
            "now_ms": now_ms,
            "elapsed_ms": max(0, now_ms - start_ms),
            "n_calls": len(rows),
            "ttft_ms": ttft,
            "tps": round(tps, 2) if tps else None,
            "out_tokens": out_tok,
            "models": models,
            "ctx_tokens": ctx,
            "phase": 1,
        }
    finally:
        conn.close()


def stream_chars(session_id, path=DB_PATH):
    """该会话最新 assistant 消息的最新 text/reasoning 部件的字符数。

    部件行在流式输出过程中被 ZCode 增量更新（time_created 到 time_updated
    持续增长），因此轮询此值的变化即可测得逐秒输出速率。
    返回 (part_id, chars) 或 None。"""
    import os

    path = os.path.expanduser(path)
    conn = _connect(path)
    try:
        mid = conn.execute(
            """SELECT id FROM message
               WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'
               ORDER BY time_created DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if not mid:
            return None
        row = conn.execute(
            """SELECT p.id, length(json_extract(p.data, '$.text'))
               FROM part p
               WHERE p.message_id = ?
                 AND json_extract(p.data, '$.type') IN ('text', 'reasoning')
               ORDER BY p.sequence DESC, p.time_created DESC LIMIT 1""",
            (mid[0],),
        ).fetchone()
        return (row[0], row[1] or 0) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def stream_chars_total(session_id, path=DB_PATH):
    """该会话全部 assistant text/reasoning 部件的字符总量（调用完成时用于校准）。"""
    import os

    path = os.path.expanduser(path)
    conn = _connect(path)
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(length(json_extract(p.data, '$.text'))), 0)
               FROM part p JOIN message m ON p.message_id = m.id
               WHERE m.session_id = ?
                 AND json_extract(m.data, '$.role') = 'assistant'
                 AND json_extract(p.data, '$.type') IN ('text', 'reasoning')""",
            (session_id,),
        ).fetchone()
        return row[0] or 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _has_running_dwf(conn, session_id, now_ms, guard_s=600):
    """该主会话是否有活跃工作流运行。

    status='running' 且 time_updated 在 guard_s 内——双条件防止宿主进程
    被杀后留下永久 running 的僵尸行把实时行钉死。旧版 ZCode 无 dwf 表时返回 False。
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM dwf_run WHERE parent_session_id = ? AND status = 'running' "
            "AND time_updated >= ? LIMIT 1",
            (session_id, now_ms - guard_s * 1000),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# 单个工作流运行的子代理聚合 SQL（LEFT JOIN 保证零调用的代理也占一行）。
# total = Σcomputed_total_tokens（每调用的入+出总消耗；GLM 通道 input 已含缓存读），
# 优于只看 output——编码类子代理输出仅占消耗的 ~1%，缓存读大头在输入侧。
_ACTOR_AGG_SQL = """
SELECT a.name, a.session_id, a.resolved_model, a.ordinal,
       COUNT(m.id),
       COALESCE(SUM(m.output_tokens), 0),
       MAX(m.completed_at),
       COALESCE(SUM(CASE WHEN m.time_to_first_token_ms IS NOT NULL
                          AND m.duration_ms - m.time_to_first_token_ms > 0
                          AND m.output_tokens > 0
                     THEN m.duration_ms - m.time_to_first_token_ms ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN m.time_to_first_token_ms IS NOT NULL
                          AND m.duration_ms IS NOT NULL
                          AND m.duration_ms - m.time_to_first_token_ms > 0
                          AND m.output_tokens > 0
                     THEN m.output_tokens ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN m.computed_total_tokens IS NOT NULL
                     THEN m.computed_total_tokens
                     ELSE m.input_tokens + m.output_tokens END), 0),
       MAX(m.model_id)
FROM dwf_actor a
LEFT JOIN model_usage m
       ON m.session_id = a.session_id
      AND m.query_source = 'workflow_child'
      AND m.status = 'completed'
WHERE a.run_id = ?
GROUP BY a.id
ORDER BY a.ordinal
"""


def workflow_live(path=DB_PATH, recent_s=20, active_s=15, stale_guard_s=600,
                  max_runs=8, parent_session_id=None):
    """进行中（或刚结束 recent_s 秒宽限内）的工作流运行，按子代理聚合。

    数据链：dwf_run(status/parent_session_id) → dwf_actor(名字/子会话)
            → model_usage(query_source='workflow_child')。
    parent_session_id 给定时只返回该主会话的运行——多窗口/自动化并存时，
    每个窗口只看自己发起的工作流，防止他窗数据串显。
    返回新→旧的运行列表（无则 []，旧版 ZCode 无 dwf 表也返回 []）：
      {run_id, run_name, parent_session_id, status, start_ms, update_ms,
       elapsed_ms, out_tokens, tps, actors, n_active,
       per_actor: [{name, model, session_id, n_calls, out_tokens, tps,
                    active, last_ms}]}

    口径：tps = Σ输出token ÷ Σ(duration−ttft)，分子分母同有效样本条件
    （ttft 齐备且 duration>ttft，无效调用两侧同剔）。这是按时长加权的调用均值，
    不是并行流的合计吞吐——合计吞吐须按共同观察窗口内到达的 token 另算，
    此处不提供。
    active 判据 = 最近 active_s 秒内有子调用完成，或存在 status='running'
    的子调用行。running 但 time_updated 超过 stale_guard_s 的运行视为
    宿主已死，不返回。
    """
    import os
    import time as _time

    path = os.path.expanduser(path)
    now_ms = int(_time.time() * 1000)
    conn = _connect(path)
    try:
        runs = conn.execute(
            """
            SELECT id, name, parent_session_id, status, time_created, time_updated
            FROM dwf_run
            WHERE ({sf}status = 'running' AND time_updated >= ?)
               OR ({sf}status != 'running' AND time_updated >= ?)
            ORDER BY time_updated DESC LIMIT ?
            """.format(sf="parent_session_id = ? AND " if parent_session_id else ""),
            # 占位符顺序：(parent, t1) OR (parent, t2) LIMIT，会话参数和时间参数交错给两份
            ((parent_session_id, now_ms - stale_guard_s * 1000,
              parent_session_id, now_ms - recent_s * 1000, max_runs)
             if parent_session_id else
             (now_ms - stale_guard_s * 1000, now_ms - recent_s * 1000, max_runs)),
        ).fetchall()
        if not runs:
            return []
        running_rows = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT session_id FROM model_usage "
                "WHERE query_source = 'workflow_child' AND status = 'running'"
            )
        }
        out = []
        for rid, name, parent, status, created, updated in runs:
            per_actor = []
            run_dec_ms = run_dec_tok = 0
            for (aname, sid, rmodel, _ord, n, out_tok, last_ms,
                 dec_ms, dec_tok, total_tok, model_id) in conn.execute(_ACTOR_AGG_SQL, (rid,)):
                model = model_id or (rmodel.rsplit("/", 1)[-1] if rmodel else None)
                tps = (dec_tok * 1000.0 / dec_ms) if dec_ms > 0 else None
                active = sid in running_rows or (
                    last_ms is not None and last_ms >= now_ms - active_s * 1000
                )
                per_actor.append({
                    "name": aname or sid.rsplit("-", 1)[-1],
                    "model": model,
                    "session_id": sid,
                    "n_calls": n,
                    "out_tokens": out_tok,
                    "total_tokens": total_tok,
                    "tps": round(tps, 2) if tps else None,
                    "active": bool(active),
                    "last_ms": last_ms,
                })
                run_dec_ms += dec_ms
                run_dec_tok += dec_tok
            tps_all = round(run_dec_tok * 1000.0 / run_dec_ms, 2) if run_dec_ms > 0 else None
            out.append({
                "run_id": rid,
                "run_name": (name or "")[:24],
                "parent_session_id": parent,
                "status": status,
                "start_ms": created,
                "update_ms": updated,
                "elapsed_ms": max(0, now_ms - (created or now_ms)),
                "out_tokens": sum(a["out_tokens"] for a in per_actor),
                "total_tokens": sum(a["total_tokens"] for a in per_actor),
                "tps": tps_all,
                "actors": len(per_actor),
                "n_active": sum(1 for a in per_actor if a["active"]),
                "per_actor": per_actor,
            })
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def session_of_message(cid, path=DB_PATH):
    """消息 id（含截断前缀、带/不带 msg_ 前缀形态）→ 所属会话 id。失败返回 None。

    渲染层把本窗口 DOM 里的 data-turn-id（即本轮 user 消息 id）报给 /live?cid=，
    服务端据此把实时数据限定在该会话内——多窗口/自动化并存时互不串显。
    """
    import os

    if not cid or not isinstance(cid, str) or len(cid) < 6:
        return None
    path = os.path.expanduser(path)
    conn = _connect(path)
    try:
        for cand in (cid, "msg_" + cid):
            r = conn.execute(
                "SELECT session_id FROM message WHERE id = ?", (cand,)
            ).fetchone()
            if r:
                return r[0]
        if len(cid) >= 8:  # DOM 属性可能是截断版：退化为前缀匹配
            for pfx in ("", "msg_"):
                r = conn.execute(
                    "SELECT session_id FROM message WHERE id LIKE ? LIMIT 1",
                    (pfx + cid + "%",),
                ).fetchone()
                if r:
                    return r[0]
        return None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


class TurnCache:
    """带 TTL 的折叠结果缓存：本地服务每个轮询周期只查一次库。"""

    def __init__(self, ttl=1.5, path=DB_PATH, limit=800):
        self.ttl = ttl
        self.path = path
        self.limit = limit
        self._at = 0.0
        self._turns = []

    def get(self):
        now = time.time()
        if now - self._at > self.ttl:
            try:
                self._turns = fold_turns(self.path, self.limit)
            except sqlite3.Error:
                pass  # 库忙或结构变化：沿用上次结果，绝不抛给调用方
            self._at = now
        return self._turns


if __name__ == "__main__":
    import json
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    turns = fold_turns(limit=n)
    print(json.dumps(turns, ensure_ascii=False, indent=2))
