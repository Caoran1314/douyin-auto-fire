"""抖音「热门视频转发到私信」：

流程（真机验证 2026-10）：
  1. 打开 https://www.douyin.com/hot 取热门视频链接
  2. 打开视频详情页
  3. hover [data-e2e="video-share-icon-container"] → 弹出「分享给朋友」浮层（带搜索框）
  4. 浮层搜索框填好友昵称 → 找到该好友那一行 → 点行内「分享」按钮
  5. 检查 toast / 按钮状态确认发送成功

浮层结构：根节点含 input[placeholder*=搜索]；好友行是 div，其中有个子元素文本恰为「分享」。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from playwright.async_api import Page

LOGGER = logging.getLogger("douyin_sender")

HOT_URL = "https://www.douyin.com/hot"
SHARE_ICON = '[data-e2e="video-share-icon-container"]'
PANEL_ATTR = "data-dy-share-panel"
ROW_ATTR = "data-dy-share-row"
BTN_ATTR = "data-dy-share-btn"


class ShareError(RuntimeError):
    pass


# ------------------------------------------------------------------ 热门视频
_FETCH_HOT_JS = """() => {
  const seen = new Set();
  const out = [];
  document.querySelectorAll('a[href*="/video/"]').forEach(a => {
    const m = a.href.match(/\\/video\\/(\\d+)/);
    if (!m) return;
    const url = 'https://www.douyin.com/video/' + m[1];
    if (seen.has(url)) return;
    seen.add(url);
    const text = (a.innerText || '').trim().replace(/\\n+/g, ' ').slice(0, 120);
    if (!text) return;                       // 没有标题的多半是封面小图，跳过
    out.push({ url, title: text });
  });
  return out;
}"""


async def fetch_hot_videos(page: Page, limit: int = 25) -> list[dict[str, str]]:
    """取抖音热点榜的视频列表（标题 + 链接）。"""
    await page.goto(HOT_URL, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(4_000)
    items = await page.evaluate(_FETCH_HOT_JS) or []
    cleaned: list[dict[str, str]] = []
    for it in items:
        title = re.sub(r"^\d{2}:\d{2}\s*", "", it.get("title", "")).strip()
        title = re.sub(r"^\d+(\.\d+)?万?\s*", "", title).strip()
        cleaned.append({"url": it["url"], "title": title or "热门视频"})
    LOGGER.info("热点榜取到 %d 个候选视频", len(cleaned))
    return cleaned[:limit]


# ------------------------------------------------------------------ 分享浮层
_MARK_PANEL_JS = """() => {
  let label = null;
  for (const el of document.querySelectorAll('div,span')) {
    const t = (el.innerText || '').trim();
    if (t.includes('分享给朋友') && el.children.length === 0) { label = el; break; }
  }
  if (!label) return 'no-label';
  let p = label;
  while (p && !p.querySelector('input')) p = p.parentElement;
  if (!p) return 'no-panel';
  p.setAttribute('__PANEL_ATTR__', '1');
  return 'ok';
}""".replace("__PANEL_ATTR__", PANEL_ATTR)

_MARK_ROW_JS = """(name) => {
  const panel = document.querySelector('[' + '__PANEL_ATTR__' + '="1"]');
  if (!panel) return 'no-panel';
  const rows = [...panel.querySelectorAll('div')].filter(r =>
    [...r.children].some(k => (k.innerText || '').trim() === '分享'));
  if (!rows.length) return 'no-rows';
  const norm = s => (s || '').replace(/\\s+/g, '');
  const target = norm(name);
  // 行文本形如「昵称 | 583 | 分享」，用「昵称」前缀匹配，优先最短
  const hits = rows.filter(r => norm(r.innerText).includes(target));
  if (!hits.length) return 'not-found:' + rows.slice(0, 5).map(r => norm(r.innerText).slice(0, 12)).join(',');
  hits.sort((a, b) => a.innerText.length - b.innerText.length);
  const row = hits[0];
  const btn = [...row.children].find(k => (k.innerText || '').trim() === '分享');
  row.setAttribute('__ROW_ATTR__', '1');
  btn.setAttribute('__BTN_ATTR__', '1');
  return 'ok';
}""".replace("__PANEL_ATTR__", PANEL_ATTR).replace("__ROW_ATTR__", ROW_ATTR).replace("__BTN_ATTR__", BTN_ATTR)

_TOAST_JS = """() => {
  const el = document.querySelector('[data-e2e="toast"]');
  return el ? (el.innerText || '').trim() : '';
}"""


async def open_share_panel(page: Page, timeout_ms: int = 20_000) -> None:
    """打开视频页并 hover 出分享浮层。

    实测个别视频 hover 不灵（偶发），所以分三轮尝试：真鼠标移动（带抖动）→ 点击 → 重载重试。
    """
    last = ""
    for round_no in range(3):
        icon = page.locator(SHARE_ICON).first
        try:
            await icon.wait_for(state="visible", timeout=timeout_ms if round_no == 0 else 8_000)
        except Exception as exc:  # noqa: BLE001
            last = f"分享按钮不可见({exc.__class__.__name__})"
            await page.wait_for_timeout(2_000)
            continue

        box = await icon.bounding_box()
        if box:
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            await page.mouse.move(cx, cy, steps=6)
            await page.wait_for_timeout(400)
            await page.mouse.move(cx + 2, cy + 1, steps=3)  # 抖一下，触发 hover 监听
        else:
            try:
                await icon.hover(timeout=8_000)
            except Exception:  # noqa: BLE001
                pass
        for _ in range(6):
            await page.wait_for_timeout(700)
            state = await page.evaluate(_MARK_PANEL_JS)
            if state == "ok":
                return
            last = state

        # 有些视频是「点击」才展开浮层
        try:
            await icon.click(timeout=5_000)
            for _ in range(5):
                await page.wait_for_timeout(700)
                if await page.evaluate(_MARK_PANEL_JS) == "ok":
                    return
        except Exception as exc:  # noqa: BLE001
            last = f"点击分享按钮失败({exc.__class__.__name__})"

        # 重载视频页再试一轮
        if round_no < 2:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(5_000)
            except Exception:  # noqa: BLE001
                pass
    raise ShareError(f"分享浮层没有出现（hover/点击均无效：{last}）")


async def share_video_to_friend(
    page: Page, video_url: str, friend: str, timeout_ms: int = 25_000, mark_only: bool = False
) -> None:
    """把 video_url 这个视频分享给好友 friend。成功返回，失败抛 ShareError。

    mark_only=True 时只走到「在浮层里定位到该好友那一行」为止（dry-run 用），不点分享。
    """
    await page.goto(video_url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(2_500)
    await open_share_panel(page, timeout_ms=timeout_ms)

    panel = page.locator(f'[{PANEL_ATTR}="1"]')
    # 1) 搜索好友（浮层内的输入框，注意页面顶部也有搜索框）
    search = panel.locator("input").first
    try:
        await search.click(timeout=5_000)
        await search.fill(friend)
        await page.wait_for_timeout(1_800)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("浮层搜索框操作失败，改为在列表中直接查找: %s", exc)

    # 2) 定位好友行（找不到时清空搜索再滚动列表）
    state = await page.evaluate(_MARK_ROW_JS, friend)
    if state != "ok":
        LOGGER.info("搜索结果里没直接找到「%s」（%s），清空搜索后滚动查找", friend, state)
        try:
            await search.fill("")
            await page.wait_for_timeout(1_500)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(10):
            state = await page.evaluate(_MARK_ROW_JS, friend)
            if state == "ok":
                break
            await page.evaluate(
                """() => {
                  const p = document.querySelector('div[__A__="1"]');
                  const sc = p && [...p.querySelectorAll('*')].find(n => n.scrollHeight > n.clientHeight + 30);
                  if (sc) sc.scrollTop = sc.scrollHeight; else if (p) p.scrollTop = p.scrollHeight;
                }""".replace("__A__", PANEL_ATTR)
            )
            await page.wait_for_timeout(800)
    if state != "ok":
        raise ShareError(f"分享浮层里找不到好友「{friend}」：{state}")

    if mark_only:
        LOGGER.info("dry-run：已在分享浮层定位到好友「%s」，未点击分享", friend)
        return

    # 3) 点行内「分享」按钮
    await page.locator(f'[{BTN_ATTR}="1"]').first.click(timeout=8_000)
    await page.wait_for_timeout(2_000)

    # 4) 校验结果：toast / 按钮变灰
    toast = await page.evaluate(_TOAST_JS)
    if toast and ("分享" in toast or "成功" in toast or "发送" in toast):
        LOGGER.info("分享成功（toast: %s）", toast.replace("\n", " "))
        return
    row_text = await page.evaluate(
        f"""() => {{ const r = document.querySelector('[{ROW_ATTR}="1"]'); const b = document.querySelector('[{BTN_ATTR}="1"]');
                  return {{ row: r ? (r.innerText||'').replace(/\\n+/g,' | ') : '', btn: b ? (b.innerText||'').trim() : '' }}; }}"""
    )
    if row_text["btn"] and row_text["btn"] != "分享":
        LOGGER.info("分享成功（按钮状态变为 %s）", row_text["btn"])
        return
    LOGGER.warning("分享结果不确定：toast=%r row=%r", toast, row_text)
    raise ShareError(f"点击分享后没有确认成功（toast={toast!r}, btn={row_text['btn']!r}）")


async def pick_videos(page: Page, limit: int = 25, exclude: list[str] | None = None) -> list[dict[str, str]]:
    exclude = exclude or []
    videos = await fetch_hot_videos(page, limit=limit + len(exclude) + 5)
    return [v for v in videos if v["url"] not in exclude][:limit]
