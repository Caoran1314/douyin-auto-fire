"""视频来源与分类。

能稳定读到的两个来源（2026-10 实测，headless）：
  1. 我的喜欢   https://www.douyin.com/user/self?showTab=like   （可滚动，实测 50+ 条）
  2. 抖音热点榜 https://www.douyin.com/hot                       （实测 30 条）

读不通的（别浪费时间）：
  - 搜索页 /search/<词>：headless 下 DOM 空白（URL 直开、回车、点搜索按钮都无效）
  - /aisearch、/jingxuan、/?recommend=1：同样不渲染

分类方式：按视频标题/话题关键词匹配（抖音卡片文字里带 #话题），关键词可在网页里改。
"我的喜欢" 这类不按关键词过滤，直接用喜欢列表（可另配关键词，留空=全部）。
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

from playwright.async_api import Page

LIKES_URL = "https://www.douyin.com/user/self?showTab=like"
HOT_URL = "https://www.douyin.com/hot"

# 默认关键词（网页里可编辑）
DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "美食介绍": ["美食", "探店", "菜谱", "做饭", "烘焙", "早餐", "餐厅", "料理", "厨房", "火锅", "烧烤", "甜品", "奶茶", "小吃", "食谱", "厨艺"],
    "旅游攻略": ["旅游", "旅行", "攻略", "景点", "自驾", "民宿", "露营", "徒步", "citywalk", "周边游", "打卡", "攻略篇", "古镇"],
    "宠物猫狗": ["猫", "猫咪", "狗", "狗狗", "喵", "汪", "宠物", "柯基", "布偶", "金毛", "橘猫", "撸猫", "萌宠", "铲屎官", "哈士奇"],
    "拍照相关": ["拍照", "摄影", "相机", "镜头", "构图", "出片", "调色", "写真", "拍照姿势", "修图", "光影", "滤镜", "街拍", "人像"],
    "我的喜欢": [],
}

CATEGORY_ORDER = ["美食介绍", "旅游攻略", "宠物猫狗", "拍照相关", "我的喜欢"]

_LINKS_JS = """() => {
  const seen = new Set(), out = [];
  for (const a of document.querySelectorAll('a[href*="/video/"]')) {
    const m = (a.href || '').match(/\\/video\\/(\\d+)/);
    if (!m) continue;
    const url = 'https://www.douyin.com/video/' + m[1];
    if (seen.has(url)) continue;
    seen.add(url);
    const t = (a.innerText || '').trim().replace(/\\n+/g, ' ');
    if (!t) continue;
    out.push({ url, title: t.slice(0, 160) });
  }
  return out;
}"""


async def _grab(page: Page, url: str, scrolls: int = 4, wait_ms: int = 2_000) -> list[dict[str, str]]:
    """打开页面并滚动收集视频链接（去重）。"""
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(6_000)
    found: dict[str, dict[str, str]] = {}
    for _ in range(max(1, scrolls)):
        for item in await page.evaluate(_LINKS_JS):
            found.setdefault(item["url"], item)
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(wait_ms)
    return list(found.values())


async def collect_likes(page: Page, scrolls: int = 5) -> list[dict[str, str]]:
    """我的喜欢（自己点过赞的视频）。"""
    return await _grab(page, LIKES_URL, scrolls=scrolls)


async def collect_hot(page: Page, scrolls: int = 2) -> list[dict[str, str]]:
    """抖音热点榜。"""
    return await _grab(page, HOT_URL, scrolls=scrolls)


def classify(
    items: list[dict[str, str]],
    categories: dict[str, list[str]],
    likes: list[dict[str, str]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """把视频按关键词分到各类。

    关键词为空的类型：若是「我的喜欢」→ 只取喜欢列表；其他类型 → 取全部（不过滤）。
    """
    out: dict[str, list[dict[str, str]]] = {}
    for name, keywords in categories.items():
        kws = [k for k in (keywords or []) if k]
        if not kws:
            if name == "我的喜欢":
                out[name] = list(likes) if likes is not None else list(items)
            else:
                out[name] = list(items)
            continue
        out[name] = [it for it in items if any(k in (it.get("title") or "") for k in kws)]
    return out


async def build_pool(
    page: Page,
    categories: dict[str, list[str]],
    include_likes: bool = True,
    include_hot: bool = True,
    likes_scrolls: int = 5,
    exclude: list[str] | None = None,
    shuffle: bool = True,
) -> dict[str, list[dict[str, str]]]:
    """抓取两个来源 → 合并去重 → 按类型分类 → （可选）打乱顺序。"""
    likes: list[dict[str, str]] = []
    merged: dict[str, dict[str, str]] = {}
    if include_likes:
        likes = await collect_likes(page, scrolls=likes_scrolls)
        for it in likes:
            merged.setdefault(it["url"], it)
    if include_hot:
        for it in await collect_hot(page):
            merged.setdefault(it["url"], it)
    banned = set(exclude or [])
    items = [it for it in merged.values() if it["url"] not in banned]
    kept = {it["url"] for it in items}
    likes = [it for it in likes if it["url"] in kept]
    pools = classify(items, categories, likes=likes)
    if shuffle:
        for name in pools:
            random.shuffle(pools[name])
    return pools


def default_categories(enabled: list[str] | None = None) -> dict[str, list[str]]:
    """默认全开；enabled 传类型名列表则只开这些。"""
    return {name: list(DEFAULT_KEYWORDS.get(name, [])) for name in CATEGORY_ORDER if not enabled or name in enabled}


def normalize_categories(raw: Any) -> dict[str, list[str]]:
    """把 config 里的类型配置规整成 {类型: [关键词]}，只保留已知类型。"""
    if isinstance(raw, dict) and raw:
        result: dict[str, list[str]] = {}
        for name, value in raw.items():
            if name not in DEFAULT_KEYWORDS:
                continue
            if isinstance(value, dict):
                if value.get("enabled") is False:
                    continue
                kws = [str(k).strip() for k in (value.get("keywords") or []) if str(k).strip()]
                result[name] = kws
            elif isinstance(value, list):
                result[name] = [str(k).strip() for k in value if str(k).strip()]
            elif value:
                result[name] = list(DEFAULT_KEYWORDS.get(name, []))
        return result or default_categories()
    if isinstance(raw, list) and raw:
        chosen = [str(x) for x in raw if str(x) in DEFAULT_KEYWORDS]
        return default_categories(chosen)
    return default_categories()


def save_pool_snapshot(path: Path, pools: dict[str, list[dict[str, str]]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pools, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":  # 手动调试：打印各类型命中数
    import sys

    async def _main() -> int:
        from app.browser import open_douyin
        from app.config import load_settings

        async with open_douyin(load_settings()) as session:
            pools = await build_pool(session.page, default_categories())
        for name, items in pools.items():
            print(f"{name}: {len(items)} 条")
            for it in items[:3]:
                print("   ", it["title"][:60])
        return 0

    sys.exit(asyncio.run(_main()))
