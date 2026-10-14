"""探针：完整抓取抖音私信会话列表（昵称 + 火花天数 + 类型），滚动加载全部。"""
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
    let spark = null, timeStr = null;
    for (const t of el.querySelectorAll('[class*="ConversationItemTagNextToTitleleft"]')) {
      const txt = (t.innerText || '').trim();
      const hasIcon = !!t.querySelector('img, svg');
      if (hasIcon && /^\\d{1,5}$/.test(txt)) spark = parseInt(txt, 10);
      else if (!timeStr) timeStr = txt;
    }
    const descEl = el.querySelector('[class*="ConversationItemDesc"]');
    items.push({
      name,
      spark_days: spark,
      time: timeStr,
      desc: descEl ? (descEl.innerText || '').trim().slice(0, 60) : '',
    });
  }
  return items;
}"""

SCROLL_JS = """() => {
  const item = document.querySelector('[class*="ConversationItemwrapper"]');
  if (!item) return null;
  let node = item.parentElement, target = null;
  while (node) {
    if (node.scrollHeight > node.clientHeight + 50) { target = node; break; }
    node = node.parentElement;
  }
  const box = target || document.scrollingElement;
  if (!box) return null;
  box.scrollTop = box.scrollHeight;
  return { cls: String(box.className).slice(0, 80), top: box.scrollTop, h: box.scrollHeight };
}"""


async def main() -> int:
    settings = load_settings()
    out = Path("artifacts/probe")
    out.mkdir(parents=True, exist_ok=True)
    merged: dict[str, dict] = {}
    rounds = 0
    async with open_douyin(settings) as session:
        page = session.page
        await open_private_messages(page)
        await page.wait_for_timeout(2_500)
        for _ in range(25):
            rounds += 1
            for it in await page.evaluate(EXTRACT_JS):
                merged.setdefault(it["name"], it)
            info = await page.evaluate(SCROLL_JS)
            await page.wait_for_timeout(1_200)
            if info is None:
                break
            # 到底了就停
            if info["top"] + 2000 >= info["h"] and rounds > 2:
                for it in await page.evaluate(EXTRACT_JS):
                    merged.setdefault(it["name"], it)
                break
        await page.screenshot(path=str(out / "chat_full.png"), full_page=False)
    items = list(merged.values())
    (out / "conversations.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    with_spark = [i for i in items if i["spark_days"]]
    with_spark.sort(key=lambda x: -x["spark_days"])
    print(f"总会话 {len(items)}，其中有火花 {len(with_spark)}（滚动 {rounds} 轮）")
    print("\n=== 有火花 ===")
    for it in with_spark:
        is_group = "：" in it["desc"] or ":" in it["desc"]
        print(f"{it['spark_days']:>5} 天 | {it['name']:<28} | {it['time']} | {'群' if is_group else '单'} | {it['desc']}")
    print("\n=== 无火花 ===")
    for it in items:
        if not it["spark_days"]:
            print(f"    -    | {it['name']:<28} | {it['time']} | {it['desc']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
