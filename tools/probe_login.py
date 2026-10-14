"""一次性探针：验证抖音 Cookie 登录态，并抓取私信页截图（不发送任何消息）。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser import open_douyin, open_private_messages, verify_login, save_trace  # noqa: E402
from app.config import load_settings  # noqa: E402


async def main() -> int:
    settings = load_settings()
    out = Path("artifacts/probe")
    out.mkdir(parents=True, exist_ok=True)
    print(f"headless={settings.headless} cookie={'set' if settings.cookie else 'missing'}")
    async with open_douyin(settings) as session:
        page = session.page
        try:
            await open_private_messages(page)
            await verify_login(page)
            print("LOGIN_OK")
        except Exception as exc:  # noqa: BLE001
            print(f"LOGIN_PROBLEM: {type(exc).__name__}: {exc}")
            await page.screenshot(path=str(out / "problem.png"), full_page=False)
            try:
                await save_trace(session, out / "probe.zip")
            except Exception as trace_exc:  # noqa: BLE001
                print(f"trace failed: {trace_exc}")
            return 1
        await page.screenshot(path=str(out / "chat.png"), full_page=False)
        print(f"url={page.url} title={await page.title()}")
        # 私信会话列表文本（用于后续配置好友昵称）
        try:
            names = await page.evaluate(
                """() => {
                    const sel = ['[class*="conversation"]','[class*="Conversation"]','[class*="session"]','[class*="Session"]'];
                    const seen = new Set();
                    const hits = [];
                    for (const s of sel) {
                        for (const el of document.querySelectorAll(s)) {
                            const t = (el.innerText || '').trim();
                            if (!t || t.length > 200) continue;
                            if (seen.has(t)) continue;
                            seen.add(t);
                            hits.push(t.replace(/\\n+/g, ' | '));
                        }
                    }
                    return hits.slice(0, 60);
                }"""
            )
            print("CONVERSATIONS:")
            for n in names:
                print(" -", n)
        except Exception as exc:  # noqa: BLE001
            print(f"list failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
