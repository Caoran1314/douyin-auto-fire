"""拉取视频池并按类型分类，输出 JSON（给网页「预览视频池」用）。

用法：
    .venv/bin/python tools/fetch_pool.py            # 用 config.json 里配的类型/关键词
    .venv/bin/python tools/fetch_pool.py --all      # 全部类型 + 默认关键词
输出（最后一行 JSON）：
    {"ok": true, "categories": {"美食介绍": {"count": 11, "samples": [{url,title},...]}, ...}, "total": 81}
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import video_sources  # noqa: E402
from app.browser import open_douyin  # noqa: E402
from app.config import load_settings  # noqa: E402


def _categories_from_config(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return video_sources.default_categories()
    vs = raw.get("video_share") or {}
    return video_sources.normalize_categories(vs.get("categories"))


async def main() -> int:
    task_config = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    settings = load_settings()
    cfg_path = task_config or Path(settings.task_config_path)
    cats = video_sources.default_categories() if "--all" in sys.argv else _categories_from_config(cfg_path)

    async with open_douyin(settings) as session:
        pools = await video_sources.build_pool(session.page, cats, exclude=[])

    out = {
        "ok": True,
        "categories": {
            name: {
                "count": len(items),
                "samples": [{"url": i["url"], "title": i["title"][:80]} for i in items[:5]],
            }
            for name, items in pools.items()
        },
        "total": len({i["url"] for items in pools.values() for i in items}),
        "keywords": cats,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
