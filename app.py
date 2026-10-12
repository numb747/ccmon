#!/usr/bin/env python3
"""
Claude Code 会话监控站 —— 在旁边另一台设备上用浏览器盯着本机所有 claude 会话。

只读：看状态、看完整对话流（prompt / 回复 / 思考 / 工具调用与结果 / 子代理），不能操作。
零依赖（Python 标准库）。会话状态判据在 ccstatus.py。

数据源:
  · 状态      `claude agents --json` + ~/.claude/sessions/*.json（经 ccstatus.py）
  · 对话全文  ~/.claude/projects/<proj>/<sessionId>.jsonl，按字节偏移增量 tail，SSE 推给浏览器
  · 子代理    ~/.claude/projects/<proj>/<sessionId>/subagents/agent-<id>.jsonl (+ .meta.json)
  ⚠ transcript 是 Claude Code 的内部格式、版本间会变。解析不认识的条目一律原样降级显示，
    每条都带行号，前端可以点开看原始 JSON —— 解析漏了也不会丢信息。

访问控制：防君子不防小人。内网谁都能打开，但要先输密码（存在 .password，明文一行）；
本机自己访问免密码（经反向代理转发来的不算，见 is_local）。输错有限速（LoginLimiter）。
登录后发一年期 cookie，改密码后旧 cookie 全部失效。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from html import escape as html_attr
from urllib.parse import parse_qs, quote, urlparse

import ccstatus as C

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("CCMON_PORT", "18767"))
PASSWORD_FILE = HERE / ".password"
COOKIE = "ccmon_auth"
COOKIE_AGE = 365 * 86400
# 本机访问免密码。反向代理和 ccmon 在同一台机器上时，外面来的请求看起来也是本机，
# 这时设 CCMON_LOCAL_NOAUTH=0 关掉，否则密码形同虚设。
LOCAL_NOAUTH = os.environ.get("CCMON_LOCAL_NOAUTH", "1") != "0"
# 带着这些头的请求是代理转发来的，不算本机。只是兜底：代理不一定会加（nginx 默认就不加）。
PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
                 "X-Real-IP", "Via")
LOGIN_WINDOW_SEC = 300    # 登录限速的统计窗口
LOGIN_MAX_PER_IP = 5      # 窗口内每个 IP 最多输错几次
LOGIN_MAX_TOTAL = 30      # 窗口内所有 IP 合计最多输错几次（挡换着 IP 猜）

PROJECTS = C.PROJECTS_DIR
HISTORY_SEC = 24 * 3600   # 侧栏「最近结束」收录多久以内动过的会话
HISTORY_MAX = 40
INITIAL_MAX = 2000        # 打开会话时最多先推多少条，更早的让用户点「全部加载」
TEXT_CAP = 30000          # 单段回复 / 思考 / prompt 的上限
RESULT_CAP = 8000         # 工具结果预览上限；全文走 /api/line 或 /api/file
INPUT_CAP = 20000         # 工具参数里单个字符串字段的上限
AGENT_POLL_SEC = 2.0      # `claude agents --json` 约 0.2s 一次，低频
BOARD_TICK_SEC = 1.0      # 状态文件很便宜，高频

SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
AGENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# 页面路由。改完 html 刷新浏览器即可，不用重启。
PAGES = {"/": "index.html", "/cats": "cats.html", "/farm": "farm.html",
         "/night": "night.html", "/v1": "index-v1.html"}
ASSET_RE = re.compile(r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.(svg|gif|png|js|mp3)$")
ASSET_TYPES = {".svg": "image/svg+xml", ".gif": "image/gif", ".png": "image/png",
               ".js": "text/javascript; charset=utf-8", ".mp3": "audio/mpeg"}
# 路径后面有时跟着字面的 \\n（嵌套转义过的输出），所以遇到反斜杠也停
PERSIST_RE = re.compile(r"Full output saved to: ([^\s\\]+)")
TAG_RE = re.compile(r"<[^>]+>")

# 这些 attachment 是每轮都会注入的上下文（系统提示词、技能清单……），量大且对「盯会话」没用，
# 只给个标签；想看的话点原始 JSON。
BULKY_ATT = {"prompt_snapshot", "skill_listing", "agent_listing_delta",
             "mcp_instructions_delta", "instructions", "deferred_tools_delta"}


# ================================================================ transcript 解析

def cap(s: str, n: int) -> tuple[str, bool]:
    return (s, False) if len(s) <= n else (s[:n], True)


def cap_input(v, n: int = INPUT_CAP):
    """递归截断工具参数里过长的字符串（Write 一个大文件时 content 能有几百 KB）。"""
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"\n…（截断，原长 {len(v)} 字符）"
    if isinstance(v, list):
        return [cap_input(x, n) for x in v]
    if isinstance(v, dict):
        return {k: cap_input(x, n) for k, x in v.items()}
    return v


def flatten(c) -> str:
    """message.content / tool_result.content 可能是字符串，也可能是块列表。"""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if not isinstance(b, dict):
                parts.append(str(b))
            elif b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append("[图片]")
            else:
                parts.append(f"[{b.get('type')}]")
        return "\n".join(parts)
    return "" if c is None else json.dumps(c, ensure_ascii=False)


def parse_line(ln: int, rec: dict) -> list[dict]:
    """一行 transcript → 零到多条前端事件。每条都带 ln（1 起的行号），供查看原始 JSON。"""
    t = rec.get("type")
    base = {"ln": ln, "ts": rec.get("timestamp")}
    out: list[dict] = []

    if t == "assistant":
        for b in (rec.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                s, cut = cap(b.get("text", ""), TEXT_CAP)
                out.append({**base, "k": "text", "text": s, "cut": cut})
            elif bt in ("thinking", "redacted_thinking"):
                s, cut = cap(b.get("thinking") or "（思考内容已加密）", TEXT_CAP)
                out.append({**base, "k": "think", "text": s, "cut": cut})
            elif bt in ("tool_use", "server_tool_use"):
                out.append({**base, "k": "tool", "id": b.get("id"), "name": b.get("name"),
                            "input": cap_input(b.get("input"))})
            else:
                out.append({**base, "k": "meta", "label": f"assistant/{bt}", "text": ""})

    elif t == "user":
        c = (rec.get("message") or {}).get("content")
        meta = bool(rec.get("isMeta") or rec.get("isCompactSummary"))
        if isinstance(c, str):
            s, cut = cap(c, TEXT_CAP)
            if meta:
                label = "压缩摘要" if rec.get("isCompactSummary") else "系统注入"
                out.append({**base, "k": "meta", "label": label, "text": s, "cut": cut})
            elif s.lstrip().startswith(("<command-", "<local-command")):
                out.append({**base, "k": "meta", "label": "斜杠命令", "text": s, "cut": cut})
            else:
                out.append({**base, "k": "prompt", "text": s, "cut": cut})
        elif isinstance(c, list):
            texts = []
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    full = flatten(b.get("content"))
                    m = PERSIST_RE.search(full[:600])
                    s, cut = cap(full, RESULT_CAP)
                    out.append({**base, "k": "result", "id": b.get("tool_use_id"),
                                "text": s, "cut": cut, "len": len(full),
                                "err": bool(b.get("is_error")),
                                "file": m.group(1) if m else None})
                elif b.get("type") == "text":
                    texts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    texts.append("[图片]")
            if texts:
                s, cut = cap("\n".join(texts), TEXT_CAP)
                out.append({**base, "k": "meta" if meta else "prompt",
                            "label": "系统注入", "text": s, "cut": cut})

    elif t == "attachment":
        a = rec.get("attachment") or {}
        at = str(a.get("type"))
        if at == "queued_command":
            s, cut = cap(str(a.get("prompt") or ""), TEXT_CAP)
            label = "后台任务回报" if s.lstrip().startswith("<task-notification>") else "插队消息"
            out.append({**base, "k": "meta", "label": label, "text": s, "cut": cut, "show": True})
        elif at in BULKY_ATT:
            out.append({**base, "k": "att", "label": at, "text": ""})
        else:
            body = {k: v for k, v in a.items() if k != "type"}
            s, cut = cap(json.dumps(body, ensure_ascii=False, indent=1), 1500)
            out.append({**base, "k": "att", "label": at, "text": s, "cut": cut,
                        "show": "hook" in at})

    elif t == "system":
        sub = rec.get("subtype") or ""
        if sub == "turn_duration":
            text = f"本轮耗时 {C.fmt_dur((rec.get('durationMs') or 0) / 1000)}"
        else:
            text = flatten(rec.get("content")) or sub
        out.append({**base, "k": "sys", "label": sub, "text": cap(text, 4000)[0]})

    elif t == "ai-title":
        out.append({**base, "k": "title", "text": rec.get("aiTitle") or ""})

    elif t == "permission-mode":
        out.append({**base, "k": "sys", "label": "mode",
                    "text": f"权限模式 → {rec.get('permissionMode')}"})

    # 其余（file-history-snapshot / last-prompt / queue-operation / mode …）是记账用的，跳过
    return out


class Tail:
    """按字节偏移增量读一个 JSONL。半行留到下次，绝不解析写了一半的行。"""

    def __init__(self, path: Path):
        self.path = path
        self.off = 0
        self.rest = b""
        self.ln = 0
        self.reset = False  # 文件变短了（被重写），调用方应让前端整体重载
        self.mode = None    # 权限模式每轮都记一次，只在真变了的时候显示

    def read_new(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.off:
            self.reset = True
            return []
        if size == self.off:
            return []
        with self.path.open("rb") as f:
            f.seek(self.off)
            data = f.read(size - self.off)
        self.off += len(data)
        lines = (self.rest + data).split(b"\n")
        self.rest = lines.pop()
        out: list[dict] = []
        for raw in lines:
            self.ln += 1
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                out.append({"ln": self.ln, "k": "meta", "label": "无法解析",
                            "text": raw[:500].decode("utf-8", "replace")})
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("type") == "permission-mode":
                if rec.get("permissionMode") == self.mode:
                    continue
                self.mode = rec.get("permissionMode")
            out.extend(parse_line(self.ln, rec))
        return out


# ================================================================ 会话清单

class MetaCache:
    """每个 transcript 的标题 / 最后一句 prompt / cwd。增量读，只解析带关键字的行。"""

    def __init__(self):
        self.d: dict[Path, dict] = {}
        self.lock = threading.Lock()

    def get(self, path: Path) -> dict:
        try:
            st = path.stat()
        except OSError:
            return {}
        with self.lock:
            m = self.d.get(path)
            if m is None or st.st_size < m["off"]:
                m = self.d[path] = {"off": 0, "rest": b"", "title": "", "prompt": "", "cwd": "",
                                    "first": "", "role": ""}
            if st.st_size > m["off"]:
                with path.open("rb") as f:
                    f.seek(m["off"])
                    data = f.read(st.st_size - m["off"])
                m["off"] += len(data)
                lines = (m["rest"] + data).split(b"\n")
                m["rest"] = lines.pop()
                for raw in lines:
                    if not (b'"ai-title"' in raw or b'"last-prompt"' in raw
                            or (not m["role"] and b'"agent-setting"' in raw)
                            or (not m["first"] and b'"type":"user"' in raw)
                            or (not m["cwd"] and b'"cwd"' in raw)):
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    t = rec.get("type")
                    if t == "ai-title":
                        m["title"] = rec.get("aiTitle") or m["title"]
                    elif t == "last-prompt":
                        m["prompt"] = (rec.get("lastPrompt") or "")[:200]
                    elif t == "agent-setting":
                        m["role"] = rec.get("agentSetting") or ""
                    elif t == "user" and not m["first"] and not rec.get("isMeta"):
                        # 团队队员的会话没有 ai-title，拿队长派的第一条任务当标题
                        c = (rec.get("message") or {}).get("content")
                        if isinstance(c, str) and not c.lstrip().startswith(("<command-", "<local-command")):
                            m["first"] = " ".join(TAG_RE.sub(" ", c).split())[:120]
                    if not m["cwd"] and rec.get("cwd"):
                        m["cwd"] = rec["cwd"]
            m["mtime"] = st.st_mtime
            return m


META = MetaCache()


def subagents_of(transcript: Path, sid: str) -> list[dict]:
    d = transcript.parent / sid / "subagents"
    out = []
    for mf in d.glob("agent-*.meta.json"):
        aid = mf.name[len("agent-"):-len(".meta.json")]
        jf = d / f"agent-{aid}.jsonl"
        try:
            meta = json.loads(mf.read_text())
            mtime = jf.stat().st_mtime
        except (OSError, ValueError):
            continue
        out.append({"id": aid, "type": meta.get("agentType") or "",
                    "desc": meta.get("description") or "",
                    "toolUseId": meta.get("toolUseId"), "mtime": mtime})
    out.sort(key=lambda x: x["mtime"])
    return out


def describe(sid: str, path: Path | None) -> dict:
    m = META.get(path) if path else {}
    title = m.get("title") or m.get("first") or ""
    if m.get("role"):
        title = f"[{m['role']}] {title}"
    return {"sid": sid, "title": title, "prompt": m.get("prompt", ""),
            "mtime": m.get("mtime"), "cwd": C.short_dir(m.get("cwd") or ""),
            "subagents": subagents_of(path, sid) if path else []}


class Board:
    """后台线程统一算状态，HTTP 请求只读缓存 —— 开几个浏览器标签都不会多调 claude。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.payload = b'{"groups":[],"history":[]}'
        self.agents: list[dict] = []
        self.err: str | None = None
        self.prev: dict = {}
        self.done_at: dict = {}
        self.hist_cache: tuple[float, list[Path]] = (0.0, [])

    def run(self):
        last_fetch = 0.0
        while True:
            now = time.time()
            if now - last_fetch >= AGENT_POLL_SEC:
                agents, err = C.fetch_agents()
                last_fetch = now
                self.err = err
                if not err:
                    self.agents = agents
            try:
                body = json.dumps(self.build(), ensure_ascii=False).encode()
                with self.lock:
                    self.payload = body
            except Exception as e:  # 单轮出错不能让线程死掉
                sys.stderr.write(f"board: {e!r}\n")
            time.sleep(BOARD_TICK_SEC)

    def recent_transcripts(self) -> list[Path]:
        ts, paths = self.hist_cache
        if time.time() - ts > 10:
            cutoff = time.time() - HISTORY_SEC
            found = []
            for p in PROJECTS.glob("*/*.jsonl"):
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt >= cutoff and SID_RE.match(p.stem):
                    found.append((mt, p))
            found.sort(reverse=True)
            paths = [p for _, p in found[:HISTORY_MAX * 2]]
            self.hist_cache = (time.time(), paths)
        return paths

    def build(self) -> dict:
        now = time.time()
        agents = C.refresh_from_files(self.agents)
        buckets = C.group_agents(agents, self.prev, self.done_at, now)
        # 「刚完成」只能靠"上一轮还是 busy"推断；把还在保鲜期里的会话的 prev 钉在 busy，
        # 否则下一轮就掉回空闲，保鲜期形同虚设。
        self.prev = {C.agent_key(a): a.get("status") for a in agents}
        for k in self.done_at:
            if self.prev.get(k) == "idle":
                self.prev[k] = "busy"

        groups, live = [], set()
        for key, icon, title in C.GROUPS:
            items = []
            for a in buckets.get(key, []):
                sid = a.get("sessionId") or ""
                live.add(sid)
                path = C.transcript_of(sid) if SID_RE.match(sid) else None
                items.append({**describe(sid, path), "pid": a.get("pid"),
                              "name": a.get("name") or "", "group": key,
                              "label": a["_label"], "since": a.get("_since"),
                              "cwd": C.short_dir(a.get("cwd") or ""),
                              "hasLog": path is not None})
            groups.append({"key": key, "icon": icon, "title": title, "items": items})

        history = []
        for p in self.recent_transcripts():
            if p.stem in live:
                continue
            history.append({**describe(p.stem, p), "group": "history", "label": "已结束",
                            "hasLog": True})
            if len(history) >= HISTORY_MAX:
                break
        return {"now": now, "err": self.err, "groups": groups, "history": history}


BOARD = Board()


# ================================================================ HTTP

def resolve_log(q: dict) -> Path | None:
    sid, agent = q.get("sid", ""), q.get("agent", "")
    if not SID_RE.match(sid):
        return None
    main = C.transcript_of(sid)
    if main is None:
        return None
    if not agent:
        return main
    if not AGENT_RE.match(agent):
        return None
    p = main.parent / sid / "subagents" / f"agent-{agent}.jsonl"
    return p if p.is_file() else None


# ================================================================ 密码

# 自动生成的密码用的字符：去掉了 0/o、1/l/i 这几个容易看错的
PW_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
# 早期版本自动生成的样子，只有一万种可能
WEAK_PW_RE = re.compile(r"^clawd\d{4}$")


def password() -> str:
    """每次请求都现读文件：改密码直接改 .password，不用重启。第一次启动自动生成一个。"""
    try:
        pw = PASSWORD_FILE.read_text().strip()
    except OSError:
        pw = ""
    if not pw:
        # 3 组 × 4 个字符，约 59 位熵；每台设备只用输一次，长一点不碍事
        pw = "-".join("".join(secrets.choice(PW_ALPHABET) for _ in range(4)) for _ in range(3))
        PASSWORD_FILE.write_text(pw + "\n")
        PASSWORD_FILE.chmod(0o600)
        print(f"生成了访问密码：{pw}（在 {PASSWORD_FILE}）", flush=True)
    return pw


def auth_token(pw: str) -> str:
    # cookie 只存密码的 HMAC，不存密码本身；密码一改，token 就对不上了
    return hmac.new(pw.encode(), b"ccmon-login-v1", hashlib.sha256).hexdigest()


class LoginLimiter:
    """登录限速。先占名额再比密码，密码对了才退回 —— 并发着猜也只拿得到窗口里剩下的那几次。

    合计上限会让猜密码的人把别人也挡在登录页外面几分钟；已经登录的设备不受影响。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.tries: list[tuple[float, str]] = []   # (时刻, ip)，只留窗口内的

    def enter(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            self.tries = [x for x in self.tries if now - x[0] < LOGIN_WINDOW_SEC]
            if (len(self.tries) >= LOGIN_MAX_TOTAL
                    or sum(1 for _, i in self.tries if i == ip) >= LOGIN_MAX_PER_IP):
                return False
            self.tries.append((now, ip))
            return True

    def clear(self, ip: str):
        with self.lock:
            self.tries = [x for x in self.tries if x[1] != ip]


LOGINS = LoginLimiter()


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ccmon · 输入密码</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #e6e7ec; color: #23263a;
         font: 15px/1.6 "Microsoft YaHei UI", "Segoe UI", "Noto Sans CJK SC", sans-serif; }
  form { width: 300px; text-align: center; }
  .pet { width: 180px; height: 120px; overflow: hidden; margin: 0 auto 6px; position: relative; }
  .pet img { position: absolute; width: 450px; height: 450px; left: -135px; top: -292px; image-rendering: pixelated; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  p { margin: 0 0 18px; color: #5b6078; font-size: 13px; }
  input, button { font: inherit; width: 100%; box-sizing: border-box; padding: 9px 12px; border: 2px solid #23263a; }
  input { background: #fff; outline: none; margin-bottom: 10px; }
  input:focus { box-shadow: 4px 4px 0 #23263a; }
  button { background: #23263a; color: #fff; cursor: pointer; }
  button:hover { background: #3a3f5c; }
  .err { color: #c2372f; margin: 0 0 10px; min-height: 1.6em; font-size: 13px; }
</style></head>
<body><form method="post" action="/login">
  <div class="pet"><img src="/login/clawd.svg" alt=""></div>
  <h1>Claude 会话监控</h1>
  <p>台式机上的 Claude 们在这里。先输个密码。</p>
  <input type="password" name="password" placeholder="密码" autofocus autocomplete="current-password">
  <input type="hidden" name="next" value="__NEXT__">
  <div class="err">__ERR__</div>
  <button type="submit">进去看看</button>
</form>
<script>
  // hash 不会发给服务器，登录跳转时自己带上，回来还能打开原来那个会话
  document.querySelector('[name=next]').value += location.hash;
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ccmon"

    def log_message(self, format, *args):  # noqa: A002 —— 与基类签名一致
        pass  # 前端每 1.5s 轮询一次，逐条记日志没意义；拒绝访问另有记录

    def client_ip(self) -> str:
        ip = self.client_address[0]
        return ip[7:] if ip.startswith("::ffff:") else ip

    def is_local(self) -> bool:
        if not LOCAL_NOAUTH or any(self.headers.get(h) for h in PROXY_HEADERS):
            return False
        ip = self.client_ip()
        if ip.startswith("127.") or ip == "::1":
            return True
        try:
            return ip == self.connection.getsockname()[0]  # 本机用内网地址访问自己
        except OSError:
            return False

    def logged_in(self) -> bool:
        if self.is_local():
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return hmac.compare_digest(v, auth_token(password()))
        return False

    def redirect(self, to: str, cookie: str | None = None):
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def safe_next(n: str) -> str:
        return n if n.startswith("/") and not n.startswith("//") else "/"

    def login_page(self, nxt: str, err: str = "", code: int = 200):
        body = (LOGIN_HTML.replace("__NEXT__", html_attr(self.safe_next(nxt)))
                .replace("__ERR__", err)).encode()
        self.send_body(code, body, "text/html; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/login":
            self.send_body(404, b"not found\n", "text/plain")
            return
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
        nxt = self.safe_next(form.get("next", "/"))
        ip = self.client_ip()
        if not LOGINS.enter(ip):
            sys.stderr.write(f"登录被限速 {ip}\n")
            self.login_page(nxt, "试得太多了，过几分钟再来", 429)
            return
        pw = password()
        if hmac.compare_digest(form.get("password", "").strip().encode(), pw.encode()):
            LOGINS.clear(ip)
            sys.stderr.write(f"登录 {ip}\n")
            self.redirect(nxt, f"{COOKIE}={auth_token(pw)}; Max-Age={COOKIE_AGE}; Path=/; HttpOnly; SameSite=Lax")
        else:
            sys.stderr.write(f"密码错误 {ip}\n")
            time.sleep(1)  # 拖慢一下乱猜
            self.login_page(nxt, "密码不对，再试一次", 401)

    def send_body(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/login":
            self.login_page(q.get("next", "/"))
            return
        if u.path == "/login/clawd.svg":  # 登录页那只 Clawd，登录前也得能看到
            self.asset_file("clawd/clawd-idle-doze.svg")
            return
        if not self.logged_in():
            if u.path.startswith("/api/"):
                self.send_body(401, b'{"error":"login required"}', "application/json")
            else:
                self.redirect("/login?next=" + quote(u.path))
            return
        try:
            if u.path in PAGES:
                self.send_body(200, (HERE / PAGES[u.path]).read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/sessions":
                with BOARD.lock:
                    body = BOARD.payload
                self.send_body(200, body, "application/json")
            elif u.path == "/api/stream":
                self.stream(q)
            elif u.path == "/api/line":
                self.raw_line(q)
            elif u.path == "/api/file":
                self.tool_file(q)
            elif u.path.startswith("/assets/"):
                self.asset_file(u.path[len("/assets/"):])
            else:
                self.send_body(404, b"not found\n", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def stream(self, q: dict):
        path = resolve_log(q)
        if path is None:
            self.send_body(404, "找不到这个会话的记录\n".encode(), "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        tail = Tail(path)
        events = tail.read_new()
        title = next((e["text"] for e in reversed(events) if e["k"] == "title"), "")
        skipped = 0
        if q.get("all") != "1" and len(events) > INITIAL_MAX:
            skipped = len(events) - INITIAL_MAX
            events = events[-INITIAL_MAX:]
        self.sse("init", {"skipped": skipped, "title": title, "events": events})

        last_send = time.time()
        while True:
            time.sleep(0.4)
            events = tail.read_new()
            if tail.reset:
                self.sse("reset", {})
                return
            if events:
                self.sse("more", {"events": events})
                last_send = time.time()
            elif time.time() - last_send > 15:
                self.wfile.write(b": ping\n\n")  # 让中间设备和浏览器知道连接还活着
                self.wfile.flush()
                last_send = time.time()

    def sse(self, event: str, data: dict):
        self.wfile.write(f"event: {event}\ndata: ".encode()
                         + json.dumps(data, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.flush()

    def raw_line(self, q: dict):
        path = resolve_log(q)
        try:
            n = int(q.get("ln", ""))
        except ValueError:
            n = 0
        if path is None or n < 1:
            self.send_body(404, b"not found\n", "text/plain")
            return
        with path.open("rb") as f:
            for i, raw in enumerate(f, 1):
                if i == n:
                    try:
                        body = json.dumps(json.loads(raw), ensure_ascii=False, indent=2).encode()
                    except ValueError:
                        body = raw
                    self.send_body(200, body, "text/plain; charset=utf-8")
                    return
        self.send_body(404, b"no such line\n", "text/plain")

    def asset_file(self, rel: str):
        """主题共用的静态素材（Clawd 的 svg、clawd.js）。只许 assets/ 下一层子目录里的白名单后缀。"""
        p = HERE / "assets" / rel
        if not ASSET_RE.match(rel) or not p.is_file():
            self.send_body(404, b"not found\n", "text/plain")
            return
        ctype = ASSET_TYPES[p.suffix]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Cache-Control", "max-age=300")  # svg 不常变，免得每次切状态都重新拉
        self.end_headers()
        self.wfile.write(p.read_bytes())

    def tool_file(self, q: dict):
        """工具输出太大时 Claude Code 会落盘到 <sid>/tool-results/，transcript 里只留 2KB 预览。"""
        p = Path(q.get("p", "/nonexistent"))
        try:
            p = p.resolve()
            ok = (p.is_relative_to(PROJECTS.resolve()) and "tool-results" in p.parts
                  and p.is_file())
        except (OSError, ValueError):
            ok = False
        if not ok:
            self.send_body(404, b"not found\n", "text/plain")
            return
        with p.open("rb") as f:
            body = f.read(8 * 1024 * 1024)
        self.send_body(200, body, "text/plain; charset=utf-8")


def main():
    threading.Thread(target=BOARD.run, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    pw = password()  # 没有密码文件就现在生成，好在日志里看到
    if WEAK_PW_RE.match(pw):
        print(f"⚠ 现在的密码是早期版本自动生成的，只有一万种可能。"
              f"建议换一个：改 {PASSWORD_FILE}，或删掉它重启让 ccmon 重新生成", flush=True)
    print(f"ccmon 监听 0.0.0.0:{PORT}，内网访问要密码（{PASSWORD_FILE}），"
          f"本机{'免密码' if LOCAL_NOAUTH else '也要密码'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
