# 架构地图变更记录

## 2026-10-13 · 51daf56 → df060b1

- 登录加了限速：新节点「登录限速 LoginLimiter」，POST /login 先占名额再比密码，超限回 429，成功后清掉该 IP 的记录（22-flow-login、30-backend、10-modules）。
- 本机免密收紧：带 X-Forwarded-For 等转发头的请求不再算本机，`CCMON_LOCAL_NOAUTH=0` 可以整个关掉免密；新节点「本机免密开关」（22-flow-login）。
- 自动生成的密码改成 xxxx-xxxx-xxxx；启动时发现旧的 clawd+4 位数字密码会打印提醒（只改了 22-flow-login 的边标签，启动提醒没画）。
- 删掉 /demos/ 路由和 Handler.demo_file（30-backend 去掉了这个节点）。
- 新增 scripts/demo-audio.py（用 ffmpeg 给宣传片配音的独立脚本，不碰运行时代码）：模块图已经满了，没画。
- 非结构改动：README、.gitignore、clawd.js 和 ccmon.service 的注释。

## 2026-10-13 · 首次生成 · 51daf56+dirty

- 首次生成：00 总览、10 模块、20–23 四条流程（看板状态、时间线推送、登录鉴权、假数据演示）、30 后端细节、31 主题页细节。
