#!/usr/bin/env bash
# douyin-auto-fire 容器入口：准备数据目录 + 启动配置网页（内含定时调度器）
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/secrets" "$DATA_DIR/artifacts"

if [ ! -f "$DATA_DIR/config.json" ]; then
  cp /app/deploy/docker/config.default.json "$DATA_DIR/config.json"
  echo "[init] 已生成默认配置 $DATA_DIR/config.json"
fi

if [ ! -f "$DATA_DIR/secrets/cookie.json" ]; then
  echo "[init] 还没有 Cookie：打开 http://<NAS_IP>:${WEB_PORT:-8600} 粘贴 Cookie-Editor 导出的 JSON"
fi

if [ -z "${WEB_PASSWORD:-}" ]; then
  echo "[warn] 未设置 WEB_PASSWORD，配置页面无密码可访问（建议设置）"
fi

exec uvicorn webui.app:app --host 0.0.0.0 --port "${WEB_PORT:-8600}" --no-access-log
