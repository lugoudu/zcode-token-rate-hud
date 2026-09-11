#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usage_db.py —— ZCode 本地用量库（SQLite）只读折叠层

数据源：~/.zcode/cli/db/db.sqlite（ZCode CLI 官方用量库，只读打开）
  - turn_usage  ：按轮聚合（含 user_message_id，作为界面 section[data-turn-id] 的桥）
  - model_usage ：按次调用明细（含 time_to_first_token_ms，用于剔除首包等待的纯解码速率）

口径（对齐 DeepSeek 风格，优于按总时长平均的粗口径）：
  - run_ms  = MAX(completed_at) - MIN(started_at)   整轮墙钟，含工具执行时段
  - ttft_ms = 本轮最早发起那一步的首 token 延迟
  - tps     = Σoutput_tokens ÷ Σ(duration_ms - ttft_ms)  仅计两值齐备的步（纯解码速率）
  - models  = 本轮用过的模型（去重，斜杠拼接用）
  - ctx     = 本轮最后一次 main_turn 调用的上下文占用近似值

过滤：model_usage 仅取 status='completed' 且 query_source='main_turn'，
      排除标题生成、压缩等旁路调用与失败重试。
"""

import sqlite3
import time

DB_PATH = "~/.zcode/cli/db/db.sqlite"

# 每轮折叠时最多回看的 model_usage 行数（覆盖上下文占用与解码时间的样本）
MODEL_SCAN_LIMIT = 12000


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)


def db_available(path=DB_PATH):
    import os
    return os.path.exists(os.path.expanduser(path))


def fold_turns(path=DB_PATH, limit=800, scan_limit=MODEL_SCAN_LIMIT):
    """
    返回按 end_ms 降序的轮列表：
      {turn_id, msg_id, session_id, status, start_ms, end_ms, run_ms,
       ttft_ms, tps, out_tokens, models, ctx_tokens}
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
        min_start = min(r[4] or 0 for r in rows)

        # 按轮折叠解码时间、模型列表，并抓每轮最后一次调用的上下文占用
        dec = {}
        models_by_turn = {}
        ctx_by_turn = {}
        for turn_id, model_id, started, decode_ms, decode_tok, ctx in conn.execute(
            """
            SELECT turn_id, model_id, started_at,
                   CASE WHEN time_to_first_token_ms IS NOT NULL
                             AND duration_ms - time_to_first_token_ms > 0
                             AND output_tokens > 0
                        THEN duration_ms - time_to_first_token_ms ELSE 0 END,
                   CASE WHEN time_to_first_token_ms IS NOT NULL AND output_tokens > 0
                        THEN output_tokens ELSE 0 END,
                   computed_total_tokens
            FROM model_usage
            WHERE status = 'completed' AND query_source = 'main_turn'
              AND started_at >= ?
            ORDER BY started_at ASC
            LIMIT ?
            """,
            (min_start, scan_limit),
        ):
            d, tok = dec.get(turn_id, (0, 0))
            dec[turn_id] = (d + (decode_ms or 0), tok + (decode_tok or 0))
            if model_id:
                lst = models_by_turn.setdefault(turn_id, [])
                if model_id not in lst:
                    lst.append(model_id)
            if ctx is not None:
                ctx_by_turn[turn_id] = ctx  # 后写覆盖，最终为本轮最后一次
    finally:
        conn.close()

    out = []
    for sid, tid, msg_id, status, started, completed, ttft, out_tok in rows:
        completed = completed or started
        decode_ms, decode_tok = dec.get(tid, (0, 0))
        tps = (decode_tok * 1000.0 / decode_ms) if decode_ms > 0 else None
        out.append(
            {
                "turn_id": tid,
                "msg_id": msg_id,
                "session_id": sid,
                "status": status,
                "start_ms": started,
                "end_ms": completed,
                "run_ms": max(0, (completed or 0) - (started or 0)),
                "ttft_ms": ttft,
                "tps": round(tps, 2) if tps else None,
                "out_tokens": decode_tok or (out_tok or 0),
                "models": models_by_turn.get(tid, []),
                "ctx_tokens": ctx_by_turn.get(tid),
            }
        )
    out.sort(key=lambda t: t["end_ms"] or 0, reverse=True)
    return out


def live_turn(path=DB_PATH, max_age_s=1200):
    """当前进行中的轮：最新 main_turn 调用所属 turn，且尚未落入 turn_usage
    （turn_usage 在轮结束时才写行，进行中的轮只能从 model_usage 聚合）。

    返回 None 或：
      {turn_id, msg_id, session_id, start_ms, now_ms, elapsed_ms, n_calls,
       ttft_ms, tps, out_tokens, models, ctx_tokens, phase}

    phase=1：本轮已有完成的模型调用（完整统计）；
    phase=0：提问已发出、首次调用尚未完成（只有开始时间，行先亮起来）。
    msg_id 桥 = 该会话最新一条 user 消息（轮未结束期间它就是本轮提问）。
    """
    import os
    import time as _time

    path = os.path.expanduser(path)
    conn = _connect(path)
    try:
        head = conn.execute(
            """SELECT turn_id, session_id, started_at FROM model_usage
               WHERE query_source = 'main_turn'
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
        now_ms = int(_time.time() * 1000)

        # phase 0 检测：全局最新一条 user 消息若（a）晚于最近一次 main_turn 调用、
        # （b）尚未被任何 turn_usage 消费、（c）足够新 —— 即为新会话首轮或老会话新一轮
        # 的“提问已发出、首步未完成”窗口，行先亮起来。
        try:
            lu = conn.execute(
                """SELECT id, session_id, time_created FROM message
                   WHERE json_extract(data, '$.role') = 'user'
                   ORDER BY time_created DESC LIMIT 1"""
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
        if not last_started or now_ms - last_started > max_age_s * 1000:
            return None  # 久无调用落库：不视为进行中
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
            return None
        start_ms = rows[0][0] or last_started
        ttft = rows[0][2]
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
