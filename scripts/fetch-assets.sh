#!/usr/bin/env bash
# 下载 Clawd 的动画 svg 和两个提示音。
#
# 这些素材来自 rullerzhou-afk/clawd-on-desk（AGPL-3.0），Clawd 形象本身是 Anthropic 的 Claude Code 吉祥物，
# 所以本仓库不直接收录，第一次运行前用这个脚本拉到本地即可。钉在一个固定提交上，免得上游改名后对不上。
set -euo pipefail
REV=af0987fb34581e241572194f62d64ef92f37c96c
BASE="https://raw.githubusercontent.com/rullerzhou-afk/clawd-on-desk/$REV"
cd "$(dirname "$0")/.."
mkdir -p assets/clawd assets/sounds

SVGS=(
  clawd-about-hero.svg
  clawd-aegyo-shy.svg
  clawd-coffee-hand.svg
  clawd-coffee-head-flip.svg
  clawd-collapse-sleep.svg
  clawd-designing.svg
  clawd-dizzy.svg
  clawd-error.svg
  clawd-happy.svg
  clawd-headphones-groove.svg
  clawd-heart-eyes.svg
  clawd-idle-bubble.svg
  clawd-idle-collapse.svg
  clawd-idle-doze.svg
  clawd-idle-follow.svg
  clawd-idle-living.svg
  clawd-idle-look.svg
  clawd-idle-low-battery.svg
  clawd-idle-reading.svg
  clawd-idle-yawn.svg
  clawd-mini-alert.svg
  clawd-mini-crabwalk.svg
  clawd-mini-enter-sleep.svg
  clawd-mini-enter.svg
  clawd-mini-happy.svg
  clawd-mini-idle.svg
  clawd-mini-peek.svg
  clawd-mini-sleep.svg
  clawd-mini-typing.svg
  clawd-notification.svg
  clawd-outlaw-bender.svg
  clawd-react-annoyed.svg
  clawd-react-double-jump.svg
  clawd-react-double.svg
  clawd-react-drag.svg
  clawd-react-left.svg
  clawd-react-right.svg
  clawd-sleeping.svg
  clawd-static-base.svg
  clawd-wake.svg
  clawd-working-building.svg
  clawd-working-carrying.svg
  clawd-working-debugger.svg
  clawd-working-juggling.svg
  clawd-working-sweeping.svg
  clawd-working-thinking.svg
  clawd-working-typing-boss.svg
  clawd-working-typing.svg
  clawd-working-ultrathink.svg
  clawd-working-wizard.svg
)
for f in "${SVGS[@]}"; do
  curl -fsSL --retry 2 -o "assets/clawd/$f" "$BASE/assets/svg/$f"
done
for f in complete.mp3 confirm.mp3; do
  curl -fsSL --retry 2 -o "assets/sounds/$f" "$BASE/assets/sounds/$f"
done
echo "已下载 ${#SVGS[@]} 个 svg 和 2 个提示音"
