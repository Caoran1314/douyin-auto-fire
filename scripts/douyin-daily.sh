#!/usr/bin/env bash
# 抖音自动续火花 —— 每日运行包装脚本（由 Hermes cron 调用）
# 约定：stdout 非空才会通知用户（no_agent cron 语义）；成功静默。
set -uo pipefail

PROJ="/你的路径/douyin-auto-fire"
PY="$PROJ/.venv/bin/python"
cd "$PROJ" || exit 0

# 1) 未配置好友（config.json 里还是占位符）→ 静默不执行；周一提醒一次
if grep -q 'PLACEHOLDER' config.json 2>/dev/null; then
  if [ "$(date +%u)" = "1" ]; then
    echo "🔥 抖音续火花：还没配置好友昵称，今天没有执行。编辑 $PROJ/config.json，把 targets[].name 换成真实好友昵称即可。"
  fi
  exit 0
fi

# 2) 正常运行
LOG="$PROJ/artifacts/cron-$(date +%Y%m%d).log"
mkdir -p "$PROJ/artifacts"
"$PY" "$PROJ/run.py" >>"$LOG" 2>&1
code=$?

if [ "$code" -ne 0 ]; then
  echo "❌ 抖音续火花执行失败（退出码 $code）。最近日志："
  tail -n 25 "$LOG" | sed 's/^/  /'
  if [ -f "$PROJ/artifacts/result.json" ]; then
    echo "--- result.json ---"
    cat "$PROJ/artifacts/result.json"
  fi
  exit 0
fi

# 3) 成功：默认静默。取消下面一行注释即可每天收到成功通知。
# echo "✅ 抖音续火花完成：$(date '+%Y-%m-%d %H:%M')"
exit 0
