#!/usr/bin/env python3
"""
Claude Code 会话状态判据（从作者的终端看板 cc-watch / cc-pet 的共用逻辑里摘出来的那一部分）。
这里只放纯函数和数据采集，不含任何渲染。

数据源:
  1. `claude agents --json`         权威列表，唯一提供 waitingFor（区分"等授权"/"等回答"）
  2. ~/.claude/sessions/<pid>.json  含 statusUpdatedAt，用来算"这个状态持续多久了"；
                                    读一次约 0.5ms，可以高频轮询
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# 分组定义：(键, 图标, 中文标题)。顺序即显示顺序，也是"最需要注意"的排序。
GROUPS = [
    ("waiting", "▲", "需要你处理"),
    ("busy",    "●", "运行中"),
    ("done",    "✔", "刚完成"),
    ("idle",    "○", "空闲"),
    ("dead",    "∙", "进程已退出"),
]

# waitingFor 的中文说法（实测见过 "input needed"；其余按官方枚举翻）
WAIT_CN = {
    "permission prompt": "等你授权",
    "input needed":      "等你回答",
    "sandbox request":   "等你放行网络",
    "worker request":    "等 worker",
    "dialog open":       "对话框开着",
}

# 刚从 busy 落到 idle 之后，标记成"刚完成"保持多少秒
DONE_STICKY_SEC = 90


# ---------------------------------------------------------------- 数据采集

def fetch_agents(cwd_filter: str | None = None) -> tuple[list[dict], str | None]:
    """调 claude agents --json（约 0.2s）。返回 (列表, 错误信息)。"""
    cmd = ["claude", "agents", "--json"]
    if cwd_filter:
        cmd += ["--cwd", cwd_filter]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return [], "找不到 claude 命令"
    except subprocess.TimeoutExpired:
        return [], "claude agents 超时"
    if out.returncode != 0:
        return [], (out.stderr or "").strip()[:120] or f"退出码 {out.returncode}"
    try:
        data = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return [], "输出不是合法 JSON"
    return (data if isinstance(data, list) else []), None


def read_session_file(pid) -> dict:
    """读 ~/.claude/sessions/<pid>.json。约 0.5ms，可高频调用。读不到返回 {}。"""
    if not pid:
        return {}
    try:
        d = json.loads((SESSIONS_DIR / f"{pid}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}  # 文件可能正被写、或进程已退出
    return d if isinstance(d, dict) else {}


def status_since(pid) -> float | None:
    """当前状态是从什么时候开始的（epoch 秒）。"""
    d = read_session_file(pid)
    ts = d.get("statusUpdatedAt") or d.get("startedAt")
    return ts / 1000.0 if ts else None


def refresh_from_files(agents: list[dict]) -> list[dict]:
    """用 sessions/*.json 里的新鲜 status 覆盖列表里的旧值。

    `claude agents --json` 贵（0.2s）只能低频调，文件便宜（0.5ms）可以高频调。
    但文件里**没有 waitingFor**，所以只覆盖 status，waitingFor 保留上一次的值——
    否则会在两次 agents 轮询之间把"等你回答"退化成"等待中"，界面来回跳。
    """
    out = []
    for a in agents:
        d = read_session_file(a.get("pid"))
        if d.get("status"):
            a = {**a, "status": d["status"]}
        out.append(a)
    return out


# ---------------------------------------------------------------- 纯函数

def agent_key(a: dict) -> str:
    """会话的稳定标识。三个来源都可能缺，兜到 pid/id。"""
    return str(a.get("sessionId") or a.get("id") or a.get("pid") or "?")


def classify(a: dict, prev: dict) -> tuple[str, str]:
    """返回 (分组键, 状态文案)。prev 是上一轮的 {agent_key: status}。"""
    st = a.get("status")
    state: str = str(a.get("state") or "")  # 交互式会话没有 state，统一成空串

    if st == "waiting":
        wf = a.get("waitingFor") or ""
        return "waiting", WAIT_CN.get(wf, f"等待：{wf}" if wf else "等待中")

    if st is None:
        # 没有 status 说明进程已退出，只剩后台会话记录
        return "dead", {"done": "已完成(进程退出)", "failed": "失败",
                        "stopped": "已停止"}.get(state, state or "进程已退出")

    if st == "busy":
        return "busy", "忙碌"

    if st == "idle":
        if state == "failed":
            return "dead", "失败"
        if state == "stopped":
            return "dead", "已停止"
        if state == "done":
            return "done", "已完成"
        # 交互式会话没有 done 状态，只能靠"刚从 busy 掉下来"推断
        if prev.get(agent_key(a)) == "busy":
            return "done", "刚完成"
        return "idle", "空闲"

    return "idle", st or "?"


def fmt_dur(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "-"
    s = int(sec)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def short_dir(p: str) -> str:
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + "/"):
        return "~/" + p[len(home) + 1:]
    return p


# ---------------------------------------------------------------- 分组

def group_agents(agents: list[dict], prev: dict, done_at: dict,
                 now: float | None = None) -> dict[str, list[dict]]:
    """把会话分到 GROUPS 的各个桶里，并给每条补上 _group/_label/_since。

    done_at 是调用方持有的 {agent_key: 落到 idle 的时刻}，用来让"刚完成"保鲜一段时间。
    这个函数会就地更新它。
    """
    now = time.time() if now is None else now
    buckets: dict[str, list[dict]] = {g[0]: [] for g in GROUPS}

    for a in agents:
        key = agent_key(a)
        grp, label = classify(a, prev)

        # "刚完成"只保鲜 DONE_STICKY_SEC 秒，之后归入空闲
        if grp == "done" and a.get("status") == "idle":
            done_at.setdefault(key, now)
            if now - done_at[key] > DONE_STICKY_SEC:
                grp, label = "idle", "空闲"
        elif grp != "done":
            done_at.pop(key, None)

        since = status_since(a.get("pid"))
        if since is None:
            started = a.get("startedAt")
            since = started / 1000.0 if started else None

        buckets[grp].append({**a, "_group": grp, "_label": label, "_since": since})

    return buckets


def flatten(buckets: dict[str, list[dict]]) -> list[dict]:
    """按 GROUPS 顺序摊平成一个列表（最需要注意的在前），供上下键选择用。"""
    out = []
    for gkey, _, _ in GROUPS:
        out.extend(buckets.get(gkey, []))
    return out


# ---------------------------------------------------------------- transcript

PROJECTS_DIR = Path.home() / ".claude" / "projects"


def transcript_of(session_id: str) -> Path | None:
    """按 sessionId 找 transcript 文件。直接 glob，不去猜目录名编码规则。"""
    if not session_id:
        return None
    hits = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None
