"""深度扫描：逐个打开私信会话，读出真实火花状态（列表里看不到"未点亮/已中断"的）。

状态判定（会话内文字）：
  lit      已点亮：列表里有 🔥N
  broken   互聊中断（"互聊中断，1 天内开始互聊，且连续互聊 3 天恢复火花"）
  pending  待点亮（"再聊 N 天点亮火花" / "连续互聊 N 天" 之类）
  none     无火花
  unknown  打不开会话 / 不算

用法：
    .venv/bin/python tools/scan_sparks.py                 # 深度扫描列表里所有没有 🔥 的会话
    .venv/bin/python tools/scan_sparks.py 五条 moya        # 只扫指定昵称
输出（最后一行 JSON）：[{name, status, note, days}]
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser import open_douyin, open_private_messages  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.douyin import DouyinChat  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_conversations import scan as scan_list  # noqa: E402

READ_JS = """() => (document.body.innerText || '').replace(/\\u200b/g, '')"""

INTERRUPTED = "互聊中断"
RESUME = re.compile(r"连续互聊\s*(\d+)\s*天")
LIGHT_UP = re.compile(r"(?:再?聊|连续互聊|互聊)\s*(\d+)\s*天.{0,6}(?:点亮|恢复)?")


def judge(text: str) -> tuple[str, str, int | None]:
    if INTERRUPTED in text:
        m = RESUME.search(text)
        return "broken", "互聊中断（可恢复）", int(m.group(1)) if m else None
    if "点亮火花" in text or "火花" in text:
        m = RESUME.search(text) or LIGHT_UP.search(text)
        return "pending", "会话里提到火花（待确认）", int(m.group(1)) if m else None
    return "none", "", None


async def main() -> int:
    want = sys.argv[1:]
    settings = load_settings()
    results: list[dict] = []
    async with open_douyin(settings) as session:
        page = session.page
        await open_private_messages(page)
        await page.wait_for_timeout(2_500)

        if want:
            names = want
        else:
            await page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(2_000)
            items = await scan_list()
            names = [i["name"] for i in items if not i.get("spark_days")]
            print(f"# 需要深度扫描的会话: {len(names)}", file=sys.stderr)

        chat = DouyinChat(page, timeout_ms=20_000)
        for idx, name in enumerate(names, 1):
            row = {"name": name, "status": "unknown", "note": "", "days": None}
            try:
                await chat.open_target(name, retries=1)
                await page.wait_for_timeout(2_500)
                text = await page.evaluate(READ_JS)
                row["status"], row["note"], row["days"] = judge(text)
                row["snippet"] = " / ".join(
                    s.strip() for s in text.splitlines() if "火花" in s or "互聊" in s
                )[:120]
            except Exception as exc:  # noqa: BLE001
                row["note"] = f"打不开会话：{str(exc)[:40]}"
            print(f"# [{idx}/{len(names)}] {name}: {row['status']} {row.get('snippet','')}", file=sys.stderr)
            results.append(row)
            # 回私信页准备下一个
            try:
                await open_private_messages(page)
            except Exception:  # noqa: BLE001
                pass

    print(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
