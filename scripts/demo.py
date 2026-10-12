#!/usr/bin/env python3
"""
用编出来的假会话跑一份 ccmon，不需要装 Claude Code —— 想先看看长什么样、或者改界面时用。

    python3 scripts/demo.py            # 然后打开 http://127.0.0.1:18799/

会在临时目录里造一个假的 ~/.claude（4 个在线会话：2 个在干活、1 个闲着、1 个等你授权；4 个最近结束的），
再放一个假的 `claude` 命令，然后用这个环境启动 app.py。README 里的截图就是这样拍的。
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = os.environ.get("CCMON_PORT", "18799")


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def build(home: Path) -> Path:
    now = time.time()
    claude = home / ".claude"
    (claude / "sessions").mkdir(parents=True)

    def proj(cwd: str) -> Path:
        return claude / "projects" / cwd.replace("/", "-").replace(".", "-")

    def transcript(sid, cwd, title, t0, convo):
        d = proj(cwd)
        d.mkdir(parents=True, exist_ok=True)
        lines, t, parent = [], t0, None

        def rec(**k):
            nonlocal t, parent
            t += random.randint(4, 40)
            u = str(uuid.uuid4())
            lines.append({"parentUuid": parent, "uuid": u, "timestamp": iso(t), "sessionId": sid, "cwd": cwd, **k})
            parent = u

        for kind, *a in convo:
            if kind == "you":
                rec(type="user", message={"role": "user", "content": a[0]}, origin={"kind": "human"})
            elif kind == "think":
                rec(type="assistant", message={"role": "assistant", "content": [{"type": "thinking", "thinking": a[0]}]})
            elif kind == "say":
                rec(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": a[0]}]})
            elif kind == "tool":
                tid = "toolu_" + uuid.uuid4().hex[:20]
                rec(type="assistant", message={"role": "assistant", "content": [
                    {"type": "tool_use", "id": tid, "name": a[0], "input": a[1]}]})
                if a[2] is not None:
                    rec(type="user", message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": tid, "content": a[2]}]})
        lines.append({"type": "ai-title", "aiTitle": title, "sessionId": sid})
        last = next(c[1] for c in reversed(convo) if c[0] == "you")
        lines.append({"type": "last-prompt", "lastPrompt": last, "sessionId": sid})
        p = d / f"{sid}.jsonl"
        p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n")
        os.utime(p, (t, t))

    blog = [
        ("you", "给博客加一个 RSS 订阅，再按标签生成归档页"),
        ("think", "先看看现在的构建脚本是怎么生成文章列表的，RSS 可以复用同一份文章元数据。"),
        ("tool", "Bash", {"command": "ls src/posts | head", "description": "看看文章目录"},
         "2024-01-hello.md\n2024-03-rust-notes.md\n2024-06-trip.md"),
        ("tool", "Read", {"file_path": "/home/demo/blog/build.py"}, "def collect_posts(): ..."),
        ("say", "构建脚本里已经有 `collect_posts()`，我在它后面加两步：\n\n"
                "1. **生成 `feed.xml`**：取最近 20 篇，按 RSS 2.0 输出\n"
                "2. **生成标签页**：每个标签一个 `/tags/<name>/index.html`"),
        ("tool", "Edit", {"file_path": "/home/demo/blog/build.py", "old_string": "posts = collect_posts()",
                          "new_string": "posts = collect_posts()\nwrite_feed(posts[:20])\nwrite_tag_pages(posts)"}, "ok"),
        ("tool", "Bash", {"command": "python build.py && ls dist/tags", "description": "构建并检查标签页"}, "rust\ntravel\nnotes"),
        ("say", "RSS 和 3 个标签页都生成好了。接下来我把订阅链接加到页脚，再跑一遍链接检查。"),
        ("you", "可以，顺便把 feed 的标题改成「Demo 的博客」"),
        ("tool", "Edit", {"file_path": "/home/demo/blog/feed.py", "old_string": 'title = "Blog"',
                          "new_string": 'title = "Demo 的博客"'}, "ok"),
        ("tool", "Bash", {"command": "python -m linkcheck dist/", "description": "检查死链"}, None),
    ]
    shop = [
        ("you", "支付回调偶尔会重复入账，帮我把重试逻辑改成幂等的"),
        ("say", "我先派两个小帮手：一个去翻最近一周的回调日志找重复入账的样本，一个去读支付网关的文档确认重试规则。"),
        ("tool", "Agent", {"description": "翻回调日志找重复入账", "subagent_type": "Explore", "prompt": "..."}, None),
        ("tool", "Agent", {"description": "读网关文档里的重试规则", "subagent_type": "general-purpose", "prompt": "..."}, None),
    ]
    dots = [
        ("you", "把 Neovim 里没在用的插件清掉"),
        ("tool", "Bash", {"command": 'nvim --headless "+Lazy! sync" +qa', "description": "同步插件"}, "ok"),
        ("say", "清掉了 6 个插件，启动时间从 180ms 降到 95ms。"),
    ]
    mobile = [
        ("you", "iOS 上登录页弹出键盘会挡住密码框"),
        ("say", "要改 `Info.plist` 和登录页布局，我先跑一下模拟器复现。"),
        ("tool", "Bash", {"command": 'xcrun simctl boot "iPhone 16"', "description": "启动模拟器"}, None),
    ]
    live = [  # pid, cwd, 标题, 状态, 多少秒前进入这个状态, waitingFor, 对话, 子代理
        (41001, "/home/demo/blog", "给博客加上 RSS 订阅和标签页", "busy", 23 * 60, None, blog, []),
        (41002, "/home/demo/shop-api", "支付回调的重试逻辑重构", "busy", 4 * 60, None, shop,
         ["翻回调日志找重复入账", "读网关文档里的重试规则"]),
        (41003, "/home/demo/dotfiles", "整理 Neovim 插件配置", "idle", 7 * 60, None, dots, []),
        (41004, "/home/demo/mobile-app", "修复 iOS 登录页键盘遮挡", "waiting", 50, "permission prompt", mobile, []),
    ]
    agents, subagent_logs = [], []
    for pid, cwd, title, status, ago, waiting_for, convo, subs in live:
        sid, since, started = str(uuid.uuid4()), now - ago, now - ago - 600
        transcript(sid, cwd, title, started, convo)
        name = os.path.basename(cwd)
        (claude / "sessions" / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": sid, "cwd": cwd, "startedAt": int(started * 1000), "kind": "interactive",
            "name": name, "status": status, "statusUpdatedAt": int(since * 1000)}))
        a = {"pid": pid, "cwd": cwd, "kind": "interactive", "startedAt": int(started * 1000),
             "sessionId": sid, "name": name, "status": status}
        if waiting_for:
            a["waitingFor"] = waiting_for
        agents.append(a)
        for desc in subs:
            d = proj(cwd) / sid / "subagents"
            d.mkdir(parents=True, exist_ok=True)
            aid = uuid.uuid4().hex[:17]
            (d / f"agent-{aid}.meta.json").write_text(json.dumps({"agentType": "Explore", "description": desc}, ensure_ascii=False))
            log = d / f"agent-{aid}.jsonl"
            log.write_text(json.dumps({"type": "user", "message": {"content": desc}, "timestamp": iso(now)}, ensure_ascii=False) + "\n")
            subagent_logs.append(log)
    for cwd, title, hours in [("/home/demo/blog", "把评论系统从 Disqus 换成 Giscus", 2),
                              ("/home/demo/shop-api", "升级 Django 到 5.1", 5),
                              ("/home/demo/notes", "整理读书笔记的目录结构", 9),
                              ("/home/demo", "排查 Docker 磁盘占满", 14)]:
        transcript(str(uuid.uuid4()), cwd, title, now - hours * 3600, [("you", title), ("say", "好了。")])

    # 假的 claude 命令：ccmon 只会调 `claude agents --json`
    (home / "agents.json").write_text(json.dumps(agents, ensure_ascii=False))
    bin_dir = home / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!/bin/sh\ncat '{home / 'agents.json'}'\n")
    fake.chmod(0o755)

    # 子代理「最近 8 秒有动静」才算在跑，所以一直摸一摸它们
    def keep_busy():
        while True:
            for p in subagent_logs:
                p.touch()
            time.sleep(2)
    threading.Thread(target=keep_busy, daemon=True).start()
    return bin_dir


def main():
    with tempfile.TemporaryDirectory(prefix="ccmon-demo-") as tmp:
        home = Path(tmp)
        bin_dir = build(home)
        env = {**os.environ, "HOME": str(home), "CCMON_PORT": PORT,
               "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
        print(f"演示数据在 {home}，打开 http://127.0.0.1:{PORT}/ （Ctrl+C 结束）", flush=True)
        try:
            subprocess.run([sys.executable, str(ROOT / "app.py")], env=env, check=False)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
