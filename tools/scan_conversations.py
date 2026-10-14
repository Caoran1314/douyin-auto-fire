"""扫描抖音私信会话列表，输出 JSON（最后一行）：[{name, spark_days, time, desc}]。

不发消息。供配置网页的"扫描"按钮调用，也可手动跑：
    .venv/bin/python tools/scan_conversations.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser import open_douyin, open_private_messages  # noqa: E402
from app.config import load_settings  # noqa: E402

EXTRACT_JS = """() => {
  const items = [];
  for (const el of document.querySelectorAll('[class*="ConversationItemwrapper"]')) {
    const titleNodes = [...el.querySelectorAll('[class*="ConversationItemtitle"]')]
      .filter(n => !n.children.length && (n.innerText || '').trim());
    const name = titleNodes.length ? titleNodes[0].innerText.trim() : '';
    if (!name) continue;
    let spark = null, timeStr = null, sparkIcon = false, iconHint = '';
    for (const t of el.querySelectorAll('[class*="ConversationItemTagNextToTitleleft"]')) {
      const txt = (t.innerText || '').trim();
      const icon = t.querySelector('img, svg');
      if (icon) {
        sparkIcon = true;
        iconHint = String((icon.getAttribute && (icon.getAttribute('src') || icon.getAttribute('class'))) || '').slice(-40);
        if (/^\\d{1,5}$/.test(txt)) spark = parseInt(txt, 10);
      } else if (!timeStr) {
        timeStr = txt;
      }
    }
    const descEl = el.querySelector('[class*="ConversationItemDesc"]');
    items.push({ name, spark_days: spark, spark_icon: sparkIcon, icon_hint: iconHint, time: timeStr,
                 desc: descEl ? (descEl.innerText || '').trim().split('\\n')[0].slice(0, 60) : '' });
  }
  return items;
}"""

SCROLL_JS = """() => {
  const item = document.querySelector('[class*="ConversationItemwrapper"]');
  if (!item) return null;
  let node = item.parentElement, target = null;
  while (node) { if (node.scrollHeight > node.clientHeight + 50) { target = node; break; } node = node.parentElement; }
  const box = target || document.scrollingElement;
  if (!box) return null;
  // 一次滚一屏多一点，触发懒加载（直接跳到底会漏掉中间没渲染的条目）
  box.scrollTop = Math.min(box.scrollHeight, box.scrollTop + box.clientHeight * 0.9);
  return { top: box.scrollTop, h: box.scrollHeight, ch: box.clientHeight };
}"""


async def scan() -> list[dict]:
    settings = load_settings()
    merged: dict[str, dict] = {}
    rounds = 0
    stable = 0
    async with open_douyin(settings) as session:
        page = session.page
        await open_private_messages(page)
        await page.wait_for_timeout(2_500)
        for _ in range(40):
            rounds += 1
            before = len(merged)
            for it in await page.evaluate(EXTRACT_JS):
                merged.setdefault(it["name"], it)
            info = await page.evaluate(SCROLL_JS)
            await page.wait_for_timeout(1_100)
            if info is None:
                break
            at_bottom = info["top"] + info["ch"] + 20 >= info["h"]
            stable = stable + 1 if (at_bottom and len(merged) == before) else 0
            if stable >= 2 and rounds > 3:      # 到底且连续两轮没新增 → 结束
                for it in await page.evaluate(EXTRACT_JS):
                    merged.setdefault(it["name"], it)
                break
    items = list(merged.values())
    items.sort(key=lambda x: (-(x["spark_days"] or 0), x["name"]))
    return items


def main() -> int:
    try:
        items = asyncio.run(scan())
    except Exception as exc:  # noqa: BLE001
        print(f"扫描失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(items, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
