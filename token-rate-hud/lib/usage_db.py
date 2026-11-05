#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usage_db.py —— ZCode 本地用量库（SQLite）只读折叠层

数据源：~/.zcode/cli/db/db.sqlite（ZCode CLI 官方用量库，只读打开）
  - turn_usage  ：按轮聚合（含 user_message_id，作为界面 section[data-turn-id] 的桥）
  - model_usage ：按次调用明细（含 time_to_first_token_ms，用于剔除首包等待的纯解码速率）

口径（页脚主展示为端到端速率；tps 仅作参考值随 /turns 下发、界面不展示）：
  - run_ms  = MAX(completed_at) - MIN(started_at)   整轮墙钟，含工具执行时段
  - ttft_ms = 本轮首输出延迟 = turn_usage.first_token_at - turn_usage.started_at
              （真实库验证 494/494 全吻合；不是减 first_model_start_at——那是
              首个模型请求发出时刻，两者差出提问到首请求的间隔，结束瞬间会跳变）
  - tps_e2e = turn_usage 整轮输出 ÷ run_ms  端到端速率（含首包等待与工具执行时段）
  - wait_ms = 轮内时间线上工具开始执行前的空闲段合计（单段 ≥ WAIT_GAP_MIN_MS 才计，
              滤除毫秒级调度噪声）。工具执行区间优先取 part 表 tool 条目的
              state.time.start/end（真实库验证：与 duration_ms 偏差 ≤50ms 占
              8581/8608、零分钟级大偏差）；tool_usage.started_at 是请求登记时刻
              （模型吐出 tool_use 后宿主立即写行，早于上一调用完成、早于用户确认），
              不是执行起点——用它当区间会把权限确认等待整个当成忙碌，实测 520 轮
              wait_ms 全为 0。part 缺失时按 completed_at - duration_ms 回推执行起点
              （tool_usage 口径：completed_at - started_at 与 duration_ms 差 >1min 的
              有 46 条，最大 6.7 小时，均为确认等待）。执行区间取不到的时段视为
              未知不扣，wait_inferred=True 提示该轮空档含推断成分
  - tps_e2e_active = 整轮输出 ÷ (run_ms - wait_ms)  剔等待端到端（页脚优先展示）
  - tps     = 有效调用 Σoutput ÷ Σ(duration_ms - ttft_ms)  首输出后调用均值（参考值）。
              有效性判据分子分母同条件：ttft 非空且 ≥0、duration>ttft、输出>0，
              无效样本两侧同剔，避免只进分子抬高均值；decode_* 字段同时披露
              有效/总调用数与有效/总输出 token 及剔除原因计数
  - models  = 本轮用过的模型（去重，斜杠拼接用）

过滤：model_usage 速率与调用计数仅取 status='completed' 且 query_source='main_turn'，
      排除标题生成、压缩等旁路调用与失败重试；忙碌时间线取全部调用不限状态
      （子代理、旁路、失败/取消/重试调用同样占用机器）。
      turn_usage 列表按主会话过滤（排除 sess_dwf%/sess_subagent% 子会话轮）后再限量，
      避免 800 条上限被子会话轮挤占；聚合键为 (session_id, turn_id) 复合主键。

工作流（2026 新功能）：子代理调用 query_source='workflow_child'，落在独立
子会话（sess_dwf-dwfrun-…-actor_N_M）。关联链 dwf_run(parent_session_id)
→ dwf_actor(名字/子会话) → model_usage；fold_turns 把其用量按时间窗归并进
主会话对应轮次的 wf_* 字段（不并入主代理 tps，避免并行失真），
workflow_live() 供 /live 实时块按子代理聚合。普通 Agent 工具子代理
（query_source='subagent'）由 subagent_live() 按批次聚合：session.parent_id
定父会话，父会话 Agent 工具 part 尾注 agentId（= 子会话 id 尾段）或执行
窗口包含关系归父轮，跨轮续跑按调用批次分行。
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
       ttft_ms, wait_ms, wait_inferred, tps_e2e, tps_e2e_active, tps,
       out_tokens, calls, models,
       turn_output_tokens, main_call_output_tokens, output_ledger_diff,
       decode_valid_calls, decode_total_calls,
       decode_valid_out_tokens, decode_total_out_tokens,
       decode_drop_ttft_missing, decode_drop_ttft_negative,
       decode_drop_zero_duration, decode_drop_zero_output,
       decode_drop_not_completed,
       wf_runs, wf_actors, wf_calls, wf_out_tokens, wf_total_tokens,
       subagents, sub_calls, sub_out_tokens, sub_total_tokens}

    字段口径：
      - turn_output_tokens  轮账本（turn_usage.output_tokens）原始值
      - main_call_output_tokens  主调用账本（完成 main_turn 调用 Σoutput）
      - output_ledger_diff  两账本之差（正数=轮账本多出，通常来自旁路/未落
        明细行的消耗），端到端分子默认走轮账本，两账本不一致时上层应明示口径
      - out_tokens  端到端展示分子（轮账本优先；仅其为 NULL 时回退主调用账本，
        绝不回退 decode 有效子集——0 是合法值不触发回退）
      - wait_ms  执行前空档合计（语义见模块 docstring）
      - wait_inferred  True=该轮空档含推断成分（part 执行区间缺失走回推、
        或存在未知执行区间/轮未闭合），剔等待值此时属低置信推断
      - subagents  本轮发起的普通 Agent 工具子代理批次列表（按父会话×父轮
        归并，行结构与 workflow_live/subagent_live 的批次行同构：name, model,
        session_id, parent_session_id, turn_id, attributed_by, n_calls,
        out_tokens, total_tokens, tps, call_avg_tps, task_avg_tps, task_ms,
        active, first_ms, last_ms；attributed_by='undetermined' 的批次保持
        独立不强行归轮，故不在此列）；sub_calls/sub_out_tokens/sub_total_tokens
        为其聚合计数。与 wf_* 同规：不并入主代理的 tps/out_tokens
    """
    import os
    path = os.path.expanduser(path)
    conn = _connect(path)
    try:
        # 只读短事务：下面多次 SELECT 共享同一快照，防止轮询间隙库被写入
        # 造成轮列表与明细行错位（WAL 下读不阻塞写）
        conn.execute("BEGIN")
        # 主会话过滤先行：limit 限额只应消费在主对话轮上，子会话轮
        # （工作流 actor / Agent 子代理）由各自的归并链路接入，不占页脚行
        rows = conn.execute(
            """
            SELECT session_id, turn_id, user_message_id, status,
                   started_at, completed_at, time_to_first_token_ms, output_tokens
            FROM turn_usage
            WHERE user_message_id IS NOT NULL AND user_message_id != ''
              AND session_id NOT LIKE 'sess_dwf%'
              AND session_id NOT LIKE 'sess_subagent%'
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if not rows:
            return []
        turn_keys = {(r[0], r[1]) for r in rows}  # 复合主键 (session_id, turn_id)
        turn_ids = list({r[1] for r in rows})
        now_fold = int(time.time() * 1000)

        # 执行前空档 = 轮内时间线上工具执行开启前的空闲段（口径见模块 docstring）。
        # 全部聚合字典一律以复合键 (session_id, turn_id) 为键：turn_id 理论全局
        # 唯一，但若跨会话出现同名轮（终审反例），单键会把两会话的明细混进
        # 同一格（输出 100+900 混成 1000）。busy = 轮内忙碌时间线。
        dec = {}
        calls_by_turn = {}
        main_out_by_turn = {}   # 主调用账本：完成 main_turn 调用 Σoutput（不限有效性）
        models_by_turn = {}
        cov = {}   # (session_id, turn_id) -> decode 覆盖率/剔除原因计数
        busy = {}  # (session_id, turn_id) -> [(start_ms, end_ms, is_tool), ...]
        inferred = set()  # 空档含推断成分的轮（复合键）
        # 轮观察窗口：活动区间先裁剪进窗口再合并——旁路调用（标题生成等）
        # 越出轮终点的尾巴不污染空档判定（真实库实测 43 条 session_title 越界）；
        # 轮未闭合时窗口终点取当下，后续按 unknown 处理不产出高置信度剔等待值
        window = {}
        for r_sid, r_tid, _mid, _r_status, r_started, r_completed, _tf, _out in rows:
            window[(r_sid, r_tid)] = (r_started or 0, r_completed or now_fold)

        # 以轮为基准分块（每块 ≤400 轮）：会话与轮是两个独立去重列表，若按
        # 同一索引各切各的，会话块与轮块在多块时错位、单块时漏掉超出的轮块
        # （复审 round1 问题 2）。明细查询因此只按 turn_id 过滤、Python 端
        # 用复合键精确归属（会话条件冗余且正是错位根源）；仅 part 查询没有
        # turn_id 可用，按块内轮推导出的会话集合过滤。块内参数 ≤800 个占位符
        for i in range(0, len(turn_ids), 400):
            t_chunk = turn_ids[i:i + 400]
            t_set = set(t_chunk)
            s_chunk = sorted({r[0] for r in rows if r[1] in t_set})
            qm_t = ",".join("?" * len(t_chunk))
            qm_s = ",".join("?" * len(s_chunk))
            for sid_c, tid_c, model_id, status, started, dur, ttft_i, out in conn.execute(
                f"""
                SELECT session_id, turn_id, model_id, status, started_at, duration_ms,
                       time_to_first_token_ms, output_tokens
                FROM model_usage
                WHERE turn_id IN ({qm_t})
                  AND query_source = 'main_turn'
                ORDER BY started_at ASC
                """,
                t_chunk,
            ):
                # 复合主键精确过滤：只认本快照轮集合内的行，复合键入格
                if (sid_c, tid_c) not in turn_keys:
                    continue
                key = (sid_c, tid_c)
                c = cov.setdefault(key, {"total": 0, "valid": 0,
                                         "valid_out": 0, "total_out": 0,
                                         "ttft_missing": 0, "ttft_negative": 0,
                                         "zero_duration": 0, "zero_output": 0,
                                         "not_completed": 0})
                c["total"] += 1
                if status != "completed":
                    c["not_completed"] += 1
                if status == "completed":
                    calls_by_turn[key] = calls_by_turn.get(key, 0) + 1
                    main_out_by_turn[key] = main_out_by_turn.get(key, 0) + (out or 0)
                    c["total_out"] += out or 0
                    if ttft_i is None:
                        c["ttft_missing"] += 1
                    elif ttft_i < 0:
                        c["ttft_negative"] += 1
                    if dur is None or dur <= (ttft_i or 0):
                        c["zero_duration"] += 1
                    if (out or 0) <= 0:
                        c["zero_output"] += 1
                    if ttft_i is not None and ttft_i >= 0 and dur \
                            and (dur - ttft_i) > 0 and (out or 0) > 0:
                        c["valid"] += 1
                        c["valid_out"] += out
                        d, tok = dec.get(key, (0, 0))
                        dec[key] = (d + (dur - ttft_i), tok + out)
                if model_id:
                    lst = models_by_turn.setdefault(key, [])
                    if model_id not in lst:
                        lst.append(model_id)
            # 忙碌时间线：全部模型调用（不限 main_turn/不限状态——子代理、旁路、
            # 失败/取消/重试调用同样占用机器，滤掉会让这段时间被误当等待扣除）。
            # 区间先与轮窗口求交集、空交集丢弃——轮外区间（旁路调用早于轮开始
            # 等）的终点不得充当窗口内空档的「上一活动终点」（终审反例：
            # 窗口 [10000,20000]、轮外区间 [1000,8000]、工具 [15000,16000] 时
            # 误算 wait=7000）。无闭合依据（completed/duration 皆缺）且起点在
            # 窗口外的区间无从判断是否延伸进窗口，标 unknown 不计 busy；
            # 起点在窗口内的运行中调用用窗口终点兜底（观察依据）
            for sid_m, tid, started, dur, completed in conn.execute(
                f"""
                SELECT session_id, turn_id, started_at, duration_ms, completed_at
                FROM model_usage
                WHERE turn_id IN ({qm_t}) AND started_at IS NOT NULL
                """,
                t_chunk,
            ):
                key = (sid_m, tid)
                win = window.get(key)
                if not win:
                    continue
                if completed:
                    end = completed
                elif dur:
                    end = started + dur
                elif started >= win[0]:
                    end = win[1]
                else:
                    inferred.add(key)  # 无闭合依据且窗口外：unknown 不计
                    continue
                s_in, e_in = max(started, win[0]), min(end, win[1])
                if e_in > s_in:
                    busy.setdefault(key, []).append((s_in, e_in, False))
            # 工具执行区间：part.state.time.start/end 是确认后的真实执行段
            # （与 duration_ms 偏差 ≤50ms 占 8581/8608、零分钟级大偏差）；
            # tool_usage.started_at 是请求登记时刻（早于用户确认），仅当
            # part 缺失时按 completed_at-duration_ms 回推执行起点。
            # 两步取数避免在 part 上做无索引的相关子查询。
            tool_part = {}
            for sid_t, call_id, ps, pe in conn.execute(
                f"""
                SELECT session_id, json_extract(data, '$.callID'),
                       json_extract(data, '$.state.time.start'),
                       json_extract(data, '$.state.time.end')
                FROM part
                WHERE session_id IN ({qm_s})
                  AND json_extract(data, '$.type') = 'tool'
                  AND json_extract(data, '$.callID') IS NOT NULL
                ORDER BY rowid ASC
                """,
                s_chunk,
            ):
                # 同一 callID 极少数多 part（实测 11 组）：rowid 升序遍历，
                # 后写覆盖先写即取最新一行
                tool_part[(sid_t, call_id)] = (ps, pe)
            for sid_t, tid, call_id, t_completed, t_dur in conn.execute(
                f"""
                SELECT session_id, turn_id, tool_call_id,
                       completed_at, duration_ms
                FROM tool_usage
                WHERE turn_id IN ({qm_t})
                  AND started_at IS NOT NULL
                """,
                t_chunk,
            ):
                key = (sid_t, tid)
                win = window.get(key)
                if not win:
                    continue  # 不在本快照轮集合内（window 即轮集合的载体）
                ps, pe = tool_part.get((sid_t, call_id), (None, None))
                if ps is not None and pe is not None:
                    t_s, t_e = ps, pe
                elif t_completed and t_dur and t_dur > 0:
                    # part 缺失：completed_at-duration_ms 回推执行段
                    # （completed_at-started_at 与 duration 差 >1min 的实测 46 条，
                    # 最大 6.7 小时，均为确认等待，回推可截掉）
                    t_s, t_e = t_completed - t_dur, t_completed
                    inferred.add(key)  # 回推口径：空档带推断成分
                else:
                    t_s = t_e = None
                    inferred.add(key)  # 执行区间无法恢复：视为未知不扣
                if t_s is None or t_e is None:
                    continue
                # 与轮窗口求交集，空交集丢弃（同模型区间：轮外段不得充当
                # 空档的上一活动终点）
                t_s, t_e = max(t_s, win[0]), min(t_e, win[1])
                if t_e > t_s:
                    busy.setdefault(key, []).append((t_s, t_e, True))

        def _wait_before_tools(intervals, min_gap_ms=WAIT_GAP_MIN_MS):
            """合并时间线上由工具执行开启的空档合计（并行工具贴着前一个跑，不重复计）。

            同一刻开始的区间归为一组统一判定：组内含工具执行即从上一活动终点
            到组起点计空档——模型与工具同刻开始（实测 1205 次）时结果与
            遍历顺序无关。"""
            wait = 0
            prev_end = None
            g_start = g_end = None
            g_tool = False
            for s, e, is_tool in sorted(intervals, key=lambda x: x[0]):
                e = max(e, s)
                if g_start is None or s > g_start:
                    if g_start is not None and prev_end is not None and g_tool \
                            and g_start - prev_end >= min_gap_ms:
                        wait += g_start - prev_end
                    prev_end = g_end if prev_end is None else max(prev_end, g_end)
                    g_start, g_end, g_tool = s, e, bool(is_tool)
                else:
                    g_end = max(g_end, e)
                    g_tool = g_tool or bool(is_tool)
            if g_start is not None and prev_end is not None and g_tool \
                    and g_start - prev_end >= min_gap_ms:
                wait += g_start - prev_end
            return wait

        wait_by_turn = {key: _wait_before_tools(iv) for key, iv in busy.items()}

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
        key = (sid, tid)
        completed_eff = completed or started
        decode_ms, decode_tok = dec.get(key, (0, 0))
        tps = (decode_tok * 1000.0 / decode_ms) if decode_ms > 0 else None
        run_ms = max(0, (completed_eff or 0) - (started or 0))
        # 端到端分子用轮账本（turn_usage 口径）：ttft 为 NULL 的步算不出解码
        # 速率，但其输出确属整轮产出，墙钟也覆盖它们，不能只计 ttft 齐备的步。
        # 轮账本仅 NULL 时回退主调用账本（完整主体合计，绝不回退 decode 有效
        # 子集）；0 是合法值不触发回退（真实库 20/279 轮两账本不一致，
        # output_ledger_diff 交上层明示口径）
        main_out = main_out_by_turn.get(key, 0)
        shown_out = out_tok if out_tok is not None else main_out
        tps_e2e = (shown_out * 1000.0 / run_ms) if run_ms > 0 else None
        cv = cov.get(key)
        # 轮未闭合：以当下为观察终点的空档属低置信推断，剔等待值不产出
        turn_open = completed is None
        if turn_open:
            inferred.add(key)
        wait_ms = min(wait_by_turn.get(key, 0), run_ms) if run_ms > 0 else 0  # 防御：不超过墙钟
        active_ms = run_ms - wait_ms
        # 剔等待值仅对「有主代理完成调用」且空档无未知成分的轮产出：
        # 纯工作流/子代理轮的输出与净忙时不属于同一主体；含推断成分的空档
        # 可能把真实忙碌误扣成等待，均不足以支撑高置信度剔等待值
        _has_main = calls_by_turn.get(key, 0) > 0
        tps_e2e_active = (shown_out * 1000.0 / active_ms) \
            if (_has_main and wait_ms > 0 and active_ms > 0 and key not in inferred) else None
        out.append(
            {
                "turn_id": tid,
                "msg_id": msg_id,
                "session_id": sid,
                "status": status,
                "start_ms": started,
                "end_ms": completed_eff,
                "run_ms": run_ms,
                "ttft_ms": ttft,  # 本轮首输出延迟（first_token_at - started_at）
                "tps": round(tps, 2) if tps else None,
                "tps_e2e": round(tps_e2e, 2) if tps_e2e else None,
                "wait_ms": wait_ms,
                "wait_inferred": key in inferred,  # inferred 存复合键，勿用裸 tid 判断
                "tps_e2e_active": round(tps_e2e_active, 2) if tps_e2e_active else None,
                "out_tokens": shown_out,
                "calls": calls_by_turn.get(key, 0),
                "models": models_by_turn.get(key, []),
                "turn_output_tokens": out_tok,
                "main_call_output_tokens": main_out,
                "output_ledger_diff": (out_tok - main_out) if out_tok is not None else None,
                "decode_valid_calls": cv["valid"] if cv else 0,
                "decode_total_calls": cv["total"] if cv else 0,
                "decode_valid_out_tokens": cv["valid_out"] if cv else 0,
                "decode_total_out_tokens": cv["total_out"] if cv else 0,
                "decode_drop_ttft_missing": cv["ttft_missing"] if cv else 0,
                "decode_drop_ttft_negative": cv["ttft_negative"] if cv else 0,
                "decode_drop_zero_duration": cv["zero_duration"] if cv else 0,
                "decode_drop_zero_output": cv["zero_output"] if cv else 0,
                "decode_drop_not_completed": cv["not_completed"] if cv else 0,
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

    # 普通 Agent 工具子代理批次按（父会话 × 父轮）归并进轮载荷：批次归属由
    # subagent_live 给出（agent_ref=父会话 Agent 工具 part 尾注精确归属、
    # inferred=执行窗口推断、undetermined=多窗/无窗不猜），undetermined 批次
    # 保持独立不强行归轮；窗口放宽到覆盖整个轮列表（静态页脚看历史，不做
    # subagent_live 默认的活跃度截断）。字段与 /live 的 subagents 批次行同构
    # （本层无流式采样，故无 instant_tps）。
    sub_by_key = {}
    try:
        window_s = max(3600, (now_fold - min_start) // 1000 + 60)
        # 会话集合从 out 自身推导（自包含，不依赖上游中间变量的存废——
        # 本函数中段曾被并行重构改写，引用其局部变量有 NameError 风险）
        for pid_s in sorted({t["session_id"] for t in out}):
            for b in subagent_live(path=path, parent_session_id=pid_s, recent_s=window_s,
                                   stale_guard_s=window_s, max_sessions=1000):
                tid_b = b.get("turn_id")
                if not tid_b or b.get("attributed_by") == "undetermined":
                    continue  # 归属缺位：保持独立，不猜轮
                sub_by_key.setdefault((pid_s, tid_b), []).append(b)
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise  # 真实故障上抛：交 TurnCache 的错误通道标记 stale，不冒充成功刷新
        sub_by_key = {}  # 旧版库缺表：轮载荷照出，子代理字段为空
    for t in out:
        batches = sub_by_key.get((t["session_id"], t["turn_id"]))
        t["subagents"] = batches or []
        t["sub_calls"] = sum(b["n_calls"] for b in batches) if batches else 0
        t["sub_out_tokens"] = sum(b["out_tokens"] for b in batches) if batches else 0
        t["sub_total_tokens"] = sum(b["total_tokens"] for b in batches) if batches else 0
    out.sort(key=lambda t: t["end_ms"] or 0, reverse=True)
    return out


def live_turn(path=DB_PATH, max_age_s=1200, session_id=None):
    """当前进行中的轮：最新 main_turn 调用所属 turn，且尚未落入 turn_usage
    （turn_usage 在轮结束时才写行，进行中的轮只能从 model_usage 聚合）。

    session_id 给定时整条链路（最新调用、最新 user 消息）都限定在该会话内——
    多窗口/自动化并存时，每个窗口只看自己的轮，防止他窗数据串显。

    返回 None 或：
      {turn_id, msg_id, session_id, start_ms, now_ms, elapsed_ms, n_calls,
       ttft_ms, turn_ttft_ms, call_ttft_ms, tps, out_tokens, models, phase}

    ttft 口径：ttft_ms/turn_ttft_ms = 本轮首输出延迟 = 首个落 token 时刻 −
    轮起点（最新 user 消息时刻），与静态轮 ttft_ms 同口径，轮结束时数字不跳变；
    call_ttft_ms = 首个完成调用的 time_to_first_token_ms（调用级口径，仅供参考）。
    start_ms 即轮起点（user 消息时刻），elapsed_ms 从提问起算。

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
        # fork 幽灵防护（2026-11-05，Codex 联合定位）：fork 复制历史消息（带
        # metadata.forkOrigin、保留原始时间戳）并在创建时写入 synthetic 提醒
        # （source=fork/selection_side_chat，时间戳=fork 时刻）——这些消息在本会话
        # 永远无人消费，会把 phase-0 误判成“待回答的新提问”，令 fork 一打开就出现
        # 计时幽灵行。在候选阶段即过滤：①排除复制来源；②排除 fork 自建提醒
        # （不一刀切所有 synthetic——workflow_launch/goal-continuation 等另有语义）；
        # ③时间兜底用严格 <（正常会话存在首问与会话创建同毫秒的实测案例，<= 会误杀）。
        try:
            lu = conn.execute(
                """SELECT id, session_id, time_created FROM message
                   WHERE json_extract(data, '$.role') = 'user' {sf}
                     AND session_id NOT LIKE 'sess_dwf%'
                     AND session_id NOT LIKE 'sess_subagent%'
                     AND json_extract(data, '$.metadata.forkOrigin') IS NULL
                     AND NOT (
                         COALESCE(json_extract(data, '$.synthetic'), 0) = 1
                         AND COALESCE(json_extract(data, '$.source'), '')
                             IN ('fork', 'selection_side_chat')
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM session s
                         WHERE s.id = message.session_id
                           AND message.time_created < s.time_created
                     )
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
                    "ttft_ms": None, "turn_ttft_ms": None, "call_ttft_ms": None,
                    "tps": None, "out_tokens": 0,
                    "models": [], "phase": 0,
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
            "SELECT 1 FROM turn_usage WHERE turn_id = ? AND session_id = ? LIMIT 1",
            (tid, sid),
        ).fetchone()
        if done:
            return None  # 该轮已结束：交给页脚静态行
        rows = conn.execute(
            """SELECT started_at, duration_ms, time_to_first_token_ms, first_token_at,
                      output_tokens, model_id
               FROM model_usage
               WHERE turn_id = ? AND session_id = ?
                 AND query_source = 'main_turn' AND status = 'completed'
               ORDER BY started_at ASC""",
            (tid, sid),
        ).fetchall()
        if not rows:
            # 首次调用进行中（尚无完成行）或纯工作流等待：出骨架行，界面显示“首步生成中”。
            # 走到这里时 done 检查已排除已结束的轮，只剩进行中的情形。
            rows = []
        # 轮真实起点：最新 user 消息时刻（真实库验证 turn_usage.started_at 与
        # user 消息 time_created 483/520 差 ≤50ms、最大 1.7s）。必须先解析出
        # 轮起点再算 ttft——用调用 started_at 两种都错：最新调用时刻晚于首个
        # 落 token（多调用轮 88%，实测差 −88万～−300万 ms 被钳成 0）、首调用
        # 时刻少算提问到首请求的间隔——结束瞬间切静态行数字都会跳变
        # （复审 round1 问题 1：原实现 ttft 先算、起点后覆盖，口径未生效）
        start_ms = last_started
        msg_id = None
        try:
            r = conn.execute(
                """SELECT id, time_created FROM message
                   WHERE session_id = ? AND json_extract(data, '$.role') = 'user'
                   ORDER BY time_created DESC LIMIT 1""",
                (sid,),
            ).fetchone()
            if r:
                msg_id = r[0]
                if r[1]:
                    start_ms = r[1]  # user 消息时刻即本轮起点
        except sqlite3.Error:
            pass  # role 解析失败：不带桥，注入脚本按运行中节点绑定
        ttft = None        # 本轮首输出延迟 = 首个落 token 时刻 − 轮起点
        call_ttft = None   # 首个完成调用的 TTFT（调用口径，参考值）
        decode_ms = decode_tok = out_tok = 0
        models = []
        for _started, dur, ttft_i, first_tok, out, model in rows:
            # 解码有效性与 fold_turns 同判据（含 ttft≥0），无效样本双侧同剔
            if ttft_i is not None and ttft_i >= 0 and dur \
                    and (dur - ttft_i) > 0 and out and out > 0:
                decode_ms += dur - ttft_i
                decode_tok += out
            out_tok += out or 0
            if ttft is None and first_tok is not None:
                ttft = first_tok - start_ms
            if call_ttft is None and ttft_i is not None:
                call_ttft = ttft_i
            if model and model not in models:
                models.append(model)
        tps = (decode_tok * 1000.0 / decode_ms) if decode_ms > 0 else None
        if ttft is not None:
            ttft = max(0, ttft)  # 时钟微差防御：首输出不早于轮起点
        return {
            "turn_id": tid,
            "msg_id": msg_id,
            "session_id": sid,
            "start_ms": start_ms,
            "now_ms": now_ms,
            "elapsed_ms": max(0, now_ms - start_ms),
            "n_calls": len(rows),
            "ttft_ms": ttft,          # 本轮首输出延迟（与静态轮 ttft_ms 同口径，不跳变）
            "turn_ttft_ms": ttft,     # 同义并列：明确「轮级」口径
            "call_ttft_ms": call_ttft,  # 首个完成调用的 TTFT（调用级口径）
            "tps": round(tps, 2) if tps else None,
            "out_tokens": out_tok,
            "models": models,
            "phase": 1,
        }
    finally:
        conn.close()


def stream_chars(session_id, path=DB_PATH, message_id=None):
    """该会话指定（缺省最新）assistant 消息的采样四元组。

    以 assistant 消息为采样单位：一条消息常有多个 text/reasoning 部件
    （真实库 15529/25714 条消息部件数 >1，reasoning 增长占大头），只看最后一个
    部件会把 part 切换前新部件已有的增量当基线丢弃。部件行在流式输出过程中
    被 ZCode 增量更新，轮询同一消息的合计值变化即可测得逐秒输出速率。
    返回 (message_id, chars, ident) 或 None：
      chars  = 该消息全部 text/reasoning 部件的字符合计；
      ident  = (providerId, modelId)，取自消息行 data 本体——正在流式生成的
               消息没有完成调用行可查，身份只能来自消息自身（真实库
               assistant 消息 data 均带 providerId/modelId 字段），字段缺失
               时为 None，由调用方走降级链。
    message_id 缺省取该会话最新 assistant 消息，传入历史消息 id 也可查询。"""
    import os

    path = os.path.expanduser(path)
    conn = _connect(path)
    try:
        cols = ("id, json_extract(data, '$.providerId'), "
                "json_extract(data, '$.modelId')")
        if message_id is not None:
            row = conn.execute(
                f"""SELECT {cols} FROM message
                    WHERE id = ? AND session_id = ?
                      AND json_extract(data, '$.role') = 'assistant'""",
                (message_id, session_id),
            ).fetchone()
        else:
            row = conn.execute(
                f"""SELECT {cols} FROM message
                    WHERE session_id = ?
                      AND json_extract(data, '$.role') = 'assistant'
                    ORDER BY time_created DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        total = conn.execute(
            """SELECT COALESCE(SUM(length(json_extract(p.data, '$.text'))), 0)
               FROM part p
               WHERE p.message_id = ?
                 AND json_extract(p.data, '$.type') IN ('text', 'reasoning')""",
            (row[0],),
        ).fetchone()
        ident = (row[1], row[2]) if (row[1] or row[2]) else None
        return (row[0], total[0] or 0, ident)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def recent_completed_calls(path=DB_PATH, age_s=300, limit=40):
    """最近完成的调用（服务层流式校准的配对原料，time.time 秒基准）。

    覆盖 main_turn + subagent + workflow_child 三类来源（A3：纯文字步在
    子代理调用里占比更高，只收 main_turn 会饿死校准样本量）；排除
    session_title/compact 等杂项。每条带 assistant_message_id（配对键）、
    provider_id 与 tool_call_count——前者是校准分桶键的一半（同名模型
    实测来自多个 provider，只按 model 分桶会混通道），后者用于筛掉
    「文字＋工具参数」混合调用（token 分子含工具参数而字符分子不含，
    纳入会系统性压低校准比）。
    completed_at 游标 + 调用 id 去重由调用方负责。
    返回 [{id, session_id, assistant_message_id, output_tokens, model_id,
           provider_id, tool_call_count, completed_at}]，completed_at 降序；
    旧版库无列时返回 []。
    """
    import os

    path = os.path.expanduser(path)
    conn = _connect(path)
    try:
        return [
            dict(zip(("id", "session_id", "assistant_message_id",
                      "output_tokens", "model_id", "provider_id",
                      "tool_call_count", "completed_at"), r))
            for r in conn.execute(
                """
                SELECT id, session_id, assistant_message_id, output_tokens,
                       model_id, provider_id, tool_call_count, completed_at
                FROM model_usage
                WHERE query_source IN ('main_turn', 'subagent', 'workflow_child')
                  AND status = 'completed'
                  AND assistant_message_id IS NOT NULL AND assistant_message_id != ''
                  AND completed_at >= ?
                ORDER BY completed_at DESC LIMIT ?
                """,
                (int(time.time() * 1000) - age_s * 1000, limit),
            )
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def stream_chars_total(session_id, path=DB_PATH):
    """该会话全部 assistant text/reasoning 部件的字符总量（调用完成时用于校准）。

    保持累计口径：含历史所有 assistant 消息，是 stream_chars 单消息口径的
    超集；新消息开启时 total 跳增「已完结旧消息 + 新消息已有量」，调用方
    （tokrate 校准）按差值配 token 增量时须容忍该跳变。"""
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
# 列序：name, session_id, resolved_model, ordinal, n_calls, out_tok, last_ms,
#       dec_ms(Σduration−ttft), dec_tok, total_tok, model_id,
#       sum_dur(Σduration), first_started, max_ended
# 注意 actor 任务墙钟取调用跨度（MIN(started)→MAX(completed)）：
# dwf_actor.time_created/updated 是宿主批量登记行的时刻（实测与调用跨度
# 严重不符，如行跨度 9ms vs 调用跨度 2.7h），不能当任务墙钟
_ACTOR_AGG_SQL = """
SELECT a.name, a.session_id, a.resolved_model, a.ordinal,
       COUNT(m.id),
       COALESCE(SUM(m.output_tokens), 0),
       MAX(m.completed_at),
       COALESCE(SUM(CASE WHEN m.time_to_first_token_ms IS NOT NULL
                          AND m.time_to_first_token_ms >= 0
                          AND m.duration_ms - m.time_to_first_token_ms > 0
                          AND m.output_tokens > 0
                     THEN m.duration_ms - m.time_to_first_token_ms ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN m.time_to_first_token_ms IS NOT NULL
                          AND m.time_to_first_token_ms >= 0
                          AND m.duration_ms IS NOT NULL
                          AND m.duration_ms - m.time_to_first_token_ms > 0
                          AND m.output_tokens > 0
                     THEN m.output_tokens ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN m.computed_total_tokens IS NOT NULL
                     THEN m.computed_total_tokens
                     ELSE m.input_tokens + m.output_tokens END), 0),
       MAX(m.model_id),
       COALESCE(SUM(m.duration_ms), 0),
       MIN(m.started_at),
       MAX(COALESCE(m.completed_at, m.started_at))
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
       elapsed_ms, out_tokens, tps, decode_ref_tps, run_avg_tps, run_ms,
       actors, n_active,
       per_actor: [{name, model, session_id, n_calls, out_tokens, total_tokens,
                    tps, decode_ref_tps, call_avg_tps, task_avg_tps, task_ms,
                    active, last_ms}]}

    速率按口径分档（均为平均口径，不是并行流的合计吞吐，也非瞬时估算——
    瞬时值由服务层流式采样注入 instant_* 字段）：
      - call_avg_tps   = Σout ÷ Σduration        调用均速（含首包等待）
      - decode_ref_tps = Σout ÷ Σ(duration−ttft)  首输出后参考（= tps 并列）
      - task_avg_tps   = Σout ÷ actor 任务墙钟    （调用跨度：首调用开始→
        末调用结束，运行中 actor 终点取当下；dwf_actor 行时间戳是登记
        时刻与任务跨度不符，不可用）
      - run_avg_tps    = 全部完成子调用 Σout（每调用只属一个 actor 会话，Σ 即
        去重）÷ run 墙钟；并行流下各 actor 分母重叠，均值低于合计吞吐属正常
    decode_ref 分子分母同有效样本条件（ttft 非空且 ≥0、duration>ttft、输出>0，
    无效调用两侧同剔）。elapsed_ms/run 墙钟在 status 非 running 时冻结为
    time_updated − time_created（宿主收尾时刻），不再随 now 虚增。
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
                 dec_ms, dec_tok, total_tok, model_id,
                 sum_dur, first_started, max_ended) in conn.execute(_ACTOR_AGG_SQL, (rid,)):
                # 身份取自同一条调用（最新一条，含 running 行——按
                # COALESCE(completed_at, started_at) 取最新）：MAX(model_id) 与
                # MAX(provider_id) 是两个独立的字典序最大值，会拼出不存在的
                # 组合（审查反例：真实 Z/A、A/Z 两行算出 Z/Z）。最新行存在时
                # provider/model 整体取自该行——包括空值：最新行缺 model 时
                # 不得拿历史聚合的 model 与新 provider 配对（审查反例：新行
                # P_NEW/NULL + 旧行 A/Z 拼出 P_NEW|Z）；仅当无任何调用行时
                # 才回退聚合/manifest 值
                ident = conn.execute(
                    """SELECT provider_id, model_id FROM model_usage
                       WHERE session_id = ? AND query_source = 'workflow_child'
                       ORDER BY COALESCE(completed_at, started_at) DESC LIMIT 1""",
                    (sid,),
                ).fetchone()
                if ident:
                    provider_id, model = ident[0], ident[1]
                else:
                    provider_id = None
                    model = model_id or (rmodel.rsplit("/", 1)[-1] if rmodel else None)
                tps = (dec_tok * 1000.0 / dec_ms) if dec_ms > 0 else None
                is_running_actor = sid in running_rows
                active = is_running_actor or (
                    last_ms is not None and last_ms >= now_ms - active_s * 1000
                )
                # 速率按口径分档（均为平均口径，非并行合计吞吐、非瞬时估算）：
                #   call_avg_tps  Σout/Σduration 调用均速（含首包等待与执行时段）
                #   decode_ref_tps  Σout/Σ(duration−ttft) 首输出后参考（= tps 并列）
                #   task_avg_tps  Σout/actor 任务墙钟——终点只在确有 running 调用时
                #     取当下；active 的 15 秒展示宽限不得延长计时分母（A6 反例：
                #     已完成 actor 分母持续增长再回缩 90.91→50→100）
                call_avg = (out_tok * 1000.0 / sum_dur) if sum_dur and sum_dur > 0 else None
                task_end = now_ms if is_running_actor else (max_ended or 0)
                task_ms = max(0, task_end - first_started) \
                    if task_end and first_started else 0
                task_avg = (out_tok * 1000.0 / task_ms) if task_ms > 0 and out_tok > 0 else None
                per_actor.append({
                    "name": aname or sid.rsplit("-", 1)[-1],
                    "model": model,
                    "provider": provider_id,
                    "session_id": sid,
                    "n_calls": n,
                    "out_tokens": out_tok,
                    "total_tokens": total_tok,
                    "tps": round(tps, 2) if tps else None,
                    "decode_ref_tps": round(tps, 2) if tps else None,
                    "call_avg_tps": round(call_avg, 2) if call_avg else None,
                    "task_avg_tps": round(task_avg, 2) if task_avg else None,
                    "task_ms": task_ms,
                    "active": bool(active),
                    "last_ms": last_ms,
                })
                run_dec_ms += dec_ms
                run_dec_tok += dec_tok
            tps_all = round(run_dec_tok * 1000.0 / run_dec_ms, 2) if run_dec_ms > 0 else None
            # run 结束后 elapsed 冻结在 time_updated：status 非 running 时观察
            # 终点是宿主收尾时刻而非当下，继续用 now 会让耗时与速率虚增
            window_end = updated if (status != "running" and updated) else now_ms
            run_ms_wall = max(0, window_end - (created or window_end))
            run_out = sum(a["out_tokens"] for a in per_actor)
            # run_avg_tps：全部完成子调用（每调用只属一个 actor 会话，Σ 即去重）
            # ÷ run 墙钟；并行流下各 actor 分母重叠，均值低于合计吞吐属正常
            run_avg = (run_out * 1000.0 / run_ms_wall) if run_ms_wall > 0 and run_out > 0 else None
            out.append({
                "run_id": rid,
                "run_name": (name or "")[:24],
                "parent_session_id": parent,
                "status": status,
                "start_ms": created,
                "update_ms": updated,
                "elapsed_ms": max(0, window_end - (created or window_end)),
                "out_tokens": run_out,
                "total_tokens": sum(a["total_tokens"] for a in per_actor),
                "tps": tps_all,
                "decode_ref_tps": tps_all,
                "run_avg_tps": round(run_avg, 2) if run_avg else None,
                "run_ms": run_ms_wall,
                "actors": len(per_actor),
                "n_active": sum(1 for a in per_actor if a["active"]),
                "per_actor": per_actor,
            })
        return out
    except sqlite3.OperationalError as exc:
        # 只把「旧版 ZCode 无 dwf 表」视为无数据返空（docstring 既定行为）；
        # 其余查询故障上抛，避免服务层把失败记成成功刷新（终审顺手项）
        if "no such table" in str(exc).lower():
            return []
        raise
    finally:
        conn.close()


def subagent_live(path=DB_PATH, recent_s=60, active_s=15, stale_guard_s=1800,
                  parent_session_id=None, max_sessions=8):
    """普通 Agent 工具子代理（query_source='subagent'）的近期聚合。

    数据链（归属优先级，真实库实测 449 次调用 / 37 个子会话）：
      (a) session.parent_id → 父主会话（37/37 可用）
      (b) 父会话内 Agent 工具 part 输出尾注 agentId（= 子会话 id 的
          sess_subagent_ 之后整段，形如 agent_<uuid>）→ part.callID =
          tool_usage.tool_call_id（必须限定 session_id，tool_call_id 非全库唯一）
          → 父轮 turn_id，attributed_by='agent_ref'；同一 agent 跨轮续跑时
          尾注会命中多个窗口，按窗口分批归各自父轮，非多义
      (c) 退化：调用落在父会话某个 Agent 工具执行窗口内（part.state.time 优先）
          → attributed_by='inferred'；一条调用同时命中多个不同父轮的窗口时标
          attributed_by='undetermined' 不猜；连父会话都取不到时同标
          'undetermined'——用量照出，归属缺位明示
    一个 agent 会跨轮续跑：按调用批次归属，每（子会话 × 父轮）一行，
    不把整会话的用量挂到单一轮次。
    parent_session_id 给定时只返回该主会话发起的批次。
    返回批次列表（新→旧，无则 []；旧版 ZCode 无 subagent 会话也返回 []），
    行结构与 workflow_live 的 per_actor 同构并附归属元数据：
      {name, model, session_id, parent_session_id, turn_id, attributed_by,
       n_calls, out_tokens, total_tokens, tps, call_avg_tps, task_avg_tps,
       task_ms, active, first_ms, last_ms}

    口径：tps（decode_ref）= Σout ÷ Σ(duration−ttft)，有效样本判据 ttft 非空
    且 ≥0、duration>ttft、输出>0，两侧同剔；call_avg_tps = Σout ÷ Σduration；
    task_avg_tps = Σout ÷ 批次墙钟（首调用开始→最后调用结束，running 批次
    终点取当下）。均为平均口径非瞬时估算（瞬时值由服务层流式采样注入）。
    会话级过滤：最后活动超 stale_guard_s 且无 running 行视为宿主已死不返回；
    批次级只保留最近 recent_s 秒内有完成调用或仍在跑的批次。
    """
    import os
    import time as _time

    path = os.path.expanduser(path)
    now_ms = int(_time.time() * 1000)
    conn = _connect(path)
    try:
        # 1) 子会话锚定 + 父会话（按最后活动排序，活跃优先限量）
        sess = conn.execute(
            """SELECT s.id, s.parent_id FROM session s
               WHERE s.id IN (SELECT DISTINCT session_id FROM model_usage
                              WHERE query_source = 'subagent')"""
        ).fetchall()
        if parent_session_id:
            sess = [r for r in sess if r[1] == parent_session_id]
        if not sess:
            return []
        act = dict(conn.execute(
            """SELECT session_id, MAX(COALESCE(completed_at, started_at))
               FROM model_usage WHERE query_source = 'subagent'
               GROUP BY session_id"""
        ).fetchall())
        running_sess = {r[0] for r in conn.execute(
            "SELECT DISTINCT session_id FROM model_usage "
            "WHERE query_source = 'subagent' AND status = 'running'"
        )}
        sess = [r for r in sess
                if r[0] in running_sess
                or (act.get(r[0], 0) >= now_ms - stale_guard_s * 1000)]
        sess.sort(key=lambda r: act.get(r[0], 0), reverse=True)
        sess = sess[:max_sessions]
        if not sess:
            return []

        # 2) 父会话的 Agent 工具执行窗口（part.state.time 优先，回推兜底）。
        #    part 按 (session, callID) 去重取首行；窗口 output 一并带出供尾注匹配
        parent_ids = sorted({r[1] for r in sess if r[1]})
        agent_win = {}
        if parent_ids:
            qm_p = ",".join("?" * len(parent_ids))
            for pid, call_id, turn_id, ps, pe, t_completed, t_dur, output in conn.execute(
                f"""
                SELECT t.session_id, t.tool_call_id, t.turn_id,
                       json_extract(p.data, '$.state.time.start'),
                       json_extract(p.data, '$.state.time.end'),
                       t.completed_at, t.duration_ms,
                       json_extract(p.data, '$.state.output')
                FROM tool_usage t
                LEFT JOIN part p
                  ON p.session_id = t.session_id
                 AND json_extract(p.data, '$.type') = 'tool'
                 AND json_extract(p.data, '$.callID') = t.tool_call_id
                WHERE t.session_id IN ({qm_p}) AND t.tool_name = 'Agent'
                """,
                parent_ids,
            ):
                key = (pid, call_id)
                if key in agent_win:
                    continue  # 同 callID 多 part：留首行（窗口取值一致）
                if ps is not None and pe is not None:
                    ws, we = ps, pe
                elif t_completed and t_dur and t_dur > 0:
                    ws, we = t_completed - t_dur, t_completed
                else:
                    ws, we = None, None
                agent_win[key] = {"parent": pid, "turn": turn_id,
                                  "ws": ws, "we": we, "output": output or ""}
        win_by_parent = {}
        for w in agent_win.values():
            win_by_parent.setdefault(w["parent"], []).append(w)

        # 3) 逐子会话取全调用，按窗口归父轮分批
        out = []
        for sid_s, pid in sess:
            rows_c = conn.execute(
                """SELECT status, started_at, completed_at, duration_ms,
                          time_to_first_token_ms, output_tokens,
                          computed_total_tokens, input_tokens, agent, model_id,
                          provider_id
                   FROM model_usage
                   WHERE session_id = ? AND query_source = 'subagent'
                   ORDER BY started_at ASC""",
                (sid_s,),
            ).fetchall()
            if not rows_c:
                continue
            token = sid_s[len("sess_subagent_"):] \
                if sid_s.startswith("sess_subagent_") else None
            wins = win_by_parent.get(pid, []) if pid else []
            ref_wins = [w for w in wins if token and token in w["output"]] \
                if token else []
            batches = {}  # turn_id -> {calls:[], attributed:最高置信}
            _RANK = {"agent_ref": 0, "inferred": 1, "undetermined": 2}
            # 身份取自最新一条有身份的调用（同一行取 model+provider，防跨行拼凑）
            ident_row = next((r for r in reversed(rows_c) if r[9] or r[10]), None)
            sess_model = ident_row[9] if ident_row else None
            sess_provider = ident_row[10] if ident_row else None
            for (status, started, completed, dur, ttft_i, out_tok,
                 comp_tot, in_tok, agent, model_id, prov_id) in rows_c:
                turn_key, how = None, "undetermined"
                if ref_wins:
                    hits = [w for w in ref_wins
                            if w["ws"] is not None and w["ws"] <= (started or 0)
                            <= (w["we"] if w["we"] is not None else now_ms)]
                    turns_hit = {w["turn"] for w in hits}
                    if len(turns_hit) == 1:
                        turn_key, how = turns_hit.pop(), "agent_ref"
                if turn_key is None:
                    cand = [w for w in wins
                            if w["ws"] is not None and w["ws"] <= (started or 0)
                            <= (w["we"] if w["we"] is not None else now_ms)]
                    turns_hit = {w["turn"] for w in cand if w["turn"]}
                    if len(turns_hit) == 1:
                        turn_key, how = turns_hit.pop(), "inferred"
                    else:
                        # 多候选（含 0 候选与跨轮多窗并存）：不猜，归 undetermined 桶
                        turn_key, how = None, "undetermined"
                b = batches.setdefault(turn_key, {"calls": [], "rank": 2})
                b["calls"].append((status, started, completed, dur, ttft_i,
                                   out_tok, comp_tot, in_tok))
                b["rank"] = min(b["rank"], _RANK[how])
            for turn_key, b in batches.items():
              # 批次级隔离：单个坏批次（宿主异常行等）只跳过自身，
              # 不得炸掉整组聚合——服务层把 workflow/subagent 刷新放在
              # 同一容错块里，一处崩会连带工作流块一起失刷（终审反例）
              try:
                calls = b["calls"]
                how = next(k for k, v in _RANK.items() if v == b["rank"])
                n_done = sum(1 for c in calls if c[0] == "completed")
                out_sum = sum(c[5] or 0 for c in calls if c[0] == "completed")
                tot_sum = sum((c[6] if c[6] is not None
                               else (c[7] or 0) + (c[5] or 0)) for c in calls)
                sum_dur = dec_ms = dec_tok = 0
                first_ms = last_ms = None
                has_running = False
                for status, started, completed, dur, ttft_i, out_tok, _c, _i in calls:
                    if status == "running":
                        has_running = True
                    if started:
                        first_ms = started if first_ms is None else min(first_ms, started)
                    if completed:
                        last_ms = completed if last_ms is None else max(last_ms, completed)
                    if status != "completed":
                        continue
                    sum_dur += dur or 0
                    if ttft_i is not None and ttft_i >= 0 and dur \
                            and (dur - ttft_i) > 0 and (out_tok or 0) > 0:
                        dec_ms += dur - ttft_i
                        dec_tok += out_tok
                # 批次墙钟：首调用开始→最后调用结束；running 批次终点取当下。
                # 终点未知（全部调用未闭合，如仅剩 error 行且 completed_at 全
                # NULL）先标 unknown：不参与墙钟（task_ms=0 → task_avg=None），
                # 不做 None 参与运算（终审反例：TypeError: NoneType - int）
                task_end = now_ms if has_running else last_ms
                task_ms = max(0, task_end - first_ms) \
                    if task_end is not None and first_ms is not None else 0
                active = has_running or (last_ms is not None
                                         and last_ms >= now_ms - active_s * 1000)
                recent = has_running or (last_ms is not None
                                         and last_ms >= now_ms - recent_s * 1000)
                if not recent:
                    continue  # 冷批次：不占渲染位
                tps = (dec_tok * 1000.0 / dec_ms) if dec_ms > 0 else None
                call_avg = (out_sum * 1000.0 / sum_dur) if sum_dur > 0 else None
                task_avg = (out_sum * 1000.0 / task_ms) \
                    if task_ms > 0 and out_sum > 0 else None
                out.append({
                    "name": agent or sid_s.rsplit("_", 1)[-1],
                    "model": sess_model,
                    "provider": sess_provider,
                    "session_id": sid_s,
                    "parent_session_id": pid,
                    "turn_id": turn_key,
                    "attributed_by": how,
                    "n_calls": n_done,
                    "out_tokens": out_sum,
                    "total_tokens": tot_sum,
                    "tps": round(tps, 2) if tps else None,
                    "call_avg_tps": round(call_avg, 2) if call_avg else None,
                    "task_avg_tps": round(task_avg, 2) if task_avg else None,
                    "task_ms": task_ms,
                    "active": bool(active),
                    "first_ms": first_ms,
                    "last_ms": last_ms,
                })
              except Exception:  # noqa: BLE001 坏批次隔离：跳过自身不影响其余批次
                continue
        out.sort(key=lambda a: a["last_ms"] or 0, reverse=True)
        return out
    except sqlite3.OperationalError as exc:
        # 只把「旧版 ZCode 缺表」视为无数据返空；其余查询故障（列缺失、
        # 库损坏等）上抛——全吞成空列表会让服务层把失败记成成功刷新，
        # 旧版兼容与真实故障不可区分（终审顺手项）。subagent 会话依赖的
        # model_usage/session 均为基础表，no such table 仅出现在旧版库
        if "no such table" in str(exc).lower():
            return []
        raise
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
    """带 TTL 的折叠结果缓存：本地服务每个轮询周期只查一次库。

    失败不吞：成功时间与错误分开记录——get_meta() 返回数据年龄、stale 标记
    与最近一次错误；连续失败超过 max_stale_s 后返回空列表，不再无限保留
    旧值冒充新鲜（页脚宁缺毋假）。get() 保留旧签名（返回列表）供既有调用方。
    """

    def __init__(self, ttl=1.5, path=DB_PATH, limit=800, max_stale_s=60.0):
        self.ttl = ttl
        self.path = path
        self.limit = limit
        self.max_stale_s = max_stale_s
        self._at = 0.0    # 最近一次尝试刷新时刻（含失败，节流用）
        self._ok_at = 0.0  # 最近一次成功刷新时刻（数据年龄基准）
        self._turns = []
        self._err = None

    def get_meta(self):
        now = time.time()
        if now - self._at > self.ttl:
            try:
                self._turns = fold_turns(self.path, self.limit)
                self._ok_at = now
                self._err = None
            except sqlite3.Error as exc:
                # 库忙或结构变化：沿用上次结果但如实记录错误；
                # 陈旧上限交给 get_meta 的 stale 判定，不在此清数据
                self._err = f"{type(exc).__name__}: {exc}"
            self._at = now
        age_ms = int((now - self._ok_at) * 1000) if self._ok_at else None
        stale = self._ok_at == 0 or (now - self._ok_at) > self.max_stale_s
        return {"turns": [] if stale else self._turns, "age_ms": age_ms,
                "stale": bool(stale), "error": self._err}

    def get(self):
        return self.get_meta()["turns"]


if __name__ == "__main__":
    import json
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    turns = fold_turns(limit=n)
    print(json.dumps(turns, ensure_ascii=False, indent=2))
