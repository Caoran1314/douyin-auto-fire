"""拉取抖音热点榜视频列表，输出 JSON（最后一行）：[{"url","title"}]。

供配置网页的「拉取热点榜」按钮调用，也可手动跑：
    .venv/bin/python tools/fetch_hot.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser import open_douyin  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.video_share import fetch_hot_videos  # noqa: E402


async def main() -> list[dict[str, str]]:
    settings = load_settings()
    async with open_douyin(settings) as session:
        return await fetch_hot_videos(session.page, limit=30)


if __name__ == "__main__":
    try:
        videos = asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"拉取热点榜失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(videos, ensure_ascii=False))
