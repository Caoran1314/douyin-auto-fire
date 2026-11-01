"""douyin-auto-fire 配置网页（FastAPI）。

- 配置好友 / 消息池 / 定时时间 / Cookie
- 一键扫描：抓取私信会话列表，标出有火花的好友
- 手动触发 dry-run 或真实发送，查看日志与结果
- 内置定时调度器（每天某个时间自动跑一次）
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from app.daily_report import build_for_target, fetch_news
from app import video_sources

BASE = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("DATA_DIR", "/data"))
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Shanghai"))

CONFIG_PATH = Path(os.getenv("TASK_CONFIG", str(DATA / "config.json")))
COOKIE_PATH = Path(os.getenv("DOUYIN_COOKIE", str(DATA / "secrets" / "cookie.json")))
ARTIFACTS = Path(os.getenv("ARTIFACTS_DIR", str(DATA / "artifacts")))
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")

app = FastAPI(title="douyin-auto-fire 配置台")
SESSIONS: set[str] = set()
STATE = {"running": False, "kind": None, "started_at": None, "log": "", "returncode": None}
HOT_CACHE: dict = {"items": [], "at": 0.0}
POOL_CACHE: dict = {"data": None, "at": 0.0}
LOCK = asyncio.Lock()


# ---------------------------------------------------------------- helpers
def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def child_env() -> dict:
    env = dict(os.environ)
    env.update(
        {
            "DOUYIN_COOKIE": str(COOKIE_PATH),
            "TASK_CONFIG": str(CONFIG_PATH),
            "ARTIFACTS_DIR": str(ARTIFACTS),
            "HEADLESS": "true",
            "TRACE": "true",
        }
    )
    return env


def cookie_ok() -> tuple[bool, int]:
    data = read_json(COOKIE_PATH, None)
    if isinstance(data, list):
        return True, len([c for c in data if isinstance(c, dict) and c.get("name")])
    return False, 0


def require_auth(request: Request) -> None:
    if not WEB_PASSWORD:
        return
    token = request.cookies.get("token") or request.headers.get("x-token")
    if token not in SESSIONS:
        raise HTTPException(status_code=401, detail="未登录")


# ---------------------------------------------------------------- 调度
def schedule_cfg() -> dict:
    cfg = read_json(CONFIG_PATH, {})
    sch = cfg.get("schedule") or {}
    return {
        "enabled": bool(sch.get("enabled", False)),
        "time": str(sch.get("time", "08:00")),
    }


def next_run_at() -> str | None:
    sch = schedule_cfg()
    if not sch["enabled"]:
        return None
    try:
        hh, mm = (int(x) for x in sch["time"].split(":"))
    except Exception:
        return None
    now = datetime.now(TZ)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.isoformat()


async def run_task(kind: str, mode: str = "auto", only: str | None = None) -> None:
    """kind: dry-run | send；mode: auto | report | video | config"""
    async with LOCK:
        if STATE["running"]:
            return
        STATE.update(running=True, kind=kind, mode=mode, started_at=datetime.now(TZ).isoformat(), log="", returncode=None)
    args = [sys.executable, str(BASE / "run.py"), "--mode", mode]
    env_extra = {"ONLY_TARGET": (only or "").strip()} if only else {}
    if kind == "dry-run":
        args.append("--dry-run")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(BASE),
            env={**child_env(), **env_extra},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        STATE["log"] = out.decode("utf-8", "replace")[-20000:]
        STATE["returncode"] = proc.returncode
    except Exception as exc:  # noqa: BLE001
        STATE["log"] = f"启动失败: {exc}"
        STATE["returncode"] = -1
    finally:
        STATE["running"] = False
        STATE["finished_at"] = datetime.now(TZ).isoformat()
        try:
            log = STATE.get("log") or ""
            if STATE.get("returncode") not in (0, None) and ("登录状态失效" in log or "登录检查" in log or "AuthenticationError" in log):
                alert_dir = os.getenv("ALERT_DIR", "")
                STATE["last_auth_error"] = "登录状态失效，请到配置台重新扫码登录"
                if alert_dir:
                    write_json(Path(alert_dir) / "ALERT.json", {
                        "time": datetime.now(TZ).isoformat(),
                        "task_id": "daily-streak",
                        "total": 1,
                        "failed": 1,
                        "sent": 0,
                        "errors": ["登录状态失效"],
                        "hint": "打开配置台 → ④ 凭证与定时 → 扫码登录（或重新粘贴 Cookie）",
                    })
            else:
                STATE["last_auth_error"] = ""
        except Exception:  # noqa: BLE001
            pass


async def scheduler_loop() -> None:
    last_day = None
    while True:
        try:
            sch = schedule_cfg()
            now = datetime.now(TZ)
            if sch["enabled"] and now.strftime("%H:%M") == sch["time"] and last_day != now.date():
                last_day = now.date()
                await run_task("send")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(30)


@app.on_event("startup")
async def _startup() -> None:
    sys.path.insert(0, str(BASE))  # 让 webui 能 import app.daily_report 等
    asyncio.create_task(scheduler_loop())


def _build_messages(pool: list[str], video: dict, per_friend: int) -> list[dict]:
    """按消息池 + 视频分享占比，生成某个好友本轮要发的消息列表。"""
    texts = [{"type": "text", "content": m} for m in pool]
    enabled = bool(video.get("enabled"))
    ratio = max(0.0, min(1.0, float(video.get("ratio", 0.0))))
    choices = list(texts)
    if enabled:
        # 用 10 份权重近似占比：ratio=0.6 -> 6 份 video_share + 4 份文字
        choices += [{"type": "video_share"}] * int(round(ratio * 10))
        if not texts and int(round(ratio * 10)) == 0:
            choices = [{"type": "video_share"}]
    if not choices:
        choices = [{"type": "text", "content": "续火花 ✨"}]
    n = max(1, min(5, int(per_friend or 1)))
    if len(choices) == 1:
        return [{**choices[0]} for _ in range(n)]
    return [{"type": "random", "choices": [dict(c) for c in choices]} for _ in range(n)]


def _normalize_categories(payload: dict) -> dict:
    """网页传来的类型配置 → {类型: {"enabled": bool, "keywords": [...]}}。"""
    raw = payload.get("video_categories")
    out: dict[str, dict] = {}
    for name in video_sources.CATEGORY_ORDER:
        default_kw = list(video_sources.DEFAULT_KEYWORDS.get(name, []))
        item = (raw or {}).get(name) if isinstance(raw, dict) else None
        if isinstance(item, dict):
            enabled = item.get("enabled", True) is not False
            kws = [str(k).strip() for k in (item.get("keywords") or []) if str(k).strip()]
        elif isinstance(item, list):
            enabled = True
            kws = [str(k).strip() for k in item if str(k).strip()]
        else:
            enabled = True
            kws = default_kw
        out[name] = {"enabled": bool(enabled), "keywords": kws or default_kw}
    return out


def _normalize_video(payload: dict) -> dict:
    urls = [u.strip() for u in (payload.get("video_urls") or []) if str(u).strip()]
    exclude = [u for u in (payload.get("video_exclude") or []) if isinstance(u, str)]
    return {
        "enabled": bool(payload.get("video_enabled", True)),
        "per_friend": max(1, min(5, int(payload.get("per_friend", 1) or 1))),
        "source": payload.get("video_source", "categories") if payload.get("video_source") in {"categories", "list"} else "categories",
        "categories": _normalize_categories(payload),
        "urls": urls,
        "exclude": exclude,
    }


def _normalize_report(payload: dict) -> dict:
    return {
        "enabled": bool(payload.get("report_enabled", True)),
        "greeting": str(payload.get("report_greeting", "早上好") or "早上好").strip()[:20],
        "news_count": max(1, min(10, int(payload.get("report_news_count", 5) or 5))),
        "include_weather": bool(payload.get("report_weather", True)),
        "include_quote": bool(payload.get("report_quote", True)),
    }


# ---------------------------------------------------------------- 页面
@app.get("/healthz")
async def healthz():
    return {"ok": True, "cookie": cookie_ok()[0], "targets": len(read_json(CONFIG_PATH, {}).get("targets", []))}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    if WEB_PASSWORD and request.cookies.get("token") not in SESSIONS:
        return HTMLResponse(LOGIN_HTML.replace("__ERR__", ""))
    return HTMLResponse((Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8"))


@app.post("/api/login")
async def login(payload: dict, response: Response):
    if not WEB_PASSWORD or payload.get("password") == WEB_PASSWORD:
        token = secrets.token_urlsafe(24)
        SESSIONS.add(token)
        response.set_cookie("token", token, httponly=True, samesite="lax", max_age=30 * 86400)
        return {"ok": True}
    raise HTTPException(status_code=401, detail="密码错误")


# ---------------------------------------------------------------- API
@app.get("/api/state")
async def api_state(request: Request):
    require_auth(request)
    ok, count = cookie_ok()
    result = read_json(ARTIFACTS / "result.json", None)
    sch = schedule_cfg()
    cookie_mtime = None
    try:
        cookie_mtime = datetime.fromtimestamp(COOKIE_PATH.stat().st_mtime, TZ).isoformat()
    except Exception:  # noqa: BLE001
        pass
    return {
        "cookie_ok": ok,
        "cookie_count": count,
        "cookie_updated_at": cookie_mtime,
        "qr_status": QR.get("status"),
        "config": read_json(CONFIG_PATH, {}),
        "schedule": sch,
        "next_run": next_run_at(),
        "server_time": datetime.now(TZ).isoformat(),
        "running": STATE["running"],
        "run_kind": STATE["kind"],
        "run_started_at": STATE["started_at"],
        "run_finished_at": STATE.get("finished_at"),
        "returncode": STATE["returncode"],
        "last_result": result,
        "targets_count": len(read_json(CONFIG_PATH, {}).get("targets", [])),
        "video": read_json(CONFIG_PATH, {}).get("video_share") or {},
        "report": read_json(CONFIG_PATH, {}).get("daily_report") or {},
        "report_sent_today": (read_json(ARTIFACTS / "last_run.json", {}) or {}).get("report_date") == datetime.now(TZ).date().isoformat(),
        "video_defaults": {
            "order": list(video_sources.CATEGORY_ORDER),
            "keywords": {k: list(v) for k, v in video_sources.DEFAULT_KEYWORDS.items()},
        },
    }


@app.post("/api/config")
async def api_save_config(payload: dict, request: Request):
    require_auth(request)
    cfg = read_json(CONFIG_PATH, {})
    pool = [str(m).strip() for m in (payload.get("message_pool") or []) if str(m).strip()]
    video = _normalize_video(payload)
    report = _normalize_report(payload)
    per_friend = video["per_friend"]
    targets = []
    for t in payload.get("targets") or []:
        name = str(t.get("name", "")).strip()
        if not name:
            continue
        if not pool and not video["enabled"] and not report["enabled"]:
            raise HTTPException(status_code=400, detail=f"好友「{name}」没有可发内容（既无文字、也没开视频分享/晨报）")
        targets.append(
            {
                "name": name,
                "spark_days_at_setup": t.get("spark_days_at_setup"),
                "city": str(t.get("city", "") or "").strip() or None,
                "messages": _build_messages(pool, video, per_friend),
            }
        )
    if not targets:
        raise HTTPException(status_code=400, detail="至少配置 1 个好友")
    cfg.update(
        {
            "task_id": cfg.get("task_id", "daily-streak"),
            "timezone": "Asia/Shanghai",
            "targets": targets,
            "message_pool": pool,
            "video_share": video,
            "daily_report": report,
            "send_interval_seconds": {
                "min": int(payload.get("interval_min", 3)),
                "max": int(payload.get("interval_max", 8)),
            },
            "continue_on_error": True,
            "prevent_duplicates": bool(payload.get("prevent_duplicates", False)),
            "target_open_retries": 1,
            "target_open_timeout_seconds": 15,
            "schedule": {
                "enabled": bool(payload.get("schedule_enabled", True)),
                "time": str(payload.get("schedule_time", "08:00")),
            },
        }
    )
    write_json(CONFIG_PATH, cfg)
    return {"ok": True, "targets": len(targets), "messages_per_target": per_friend}


@app.post("/api/cookie")
async def api_save_cookie(payload: dict, request: Request):
    require_auth(request)
    raw = payload.get("cookie", "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Cookie 不是有效 JSON: {exc}") from exc
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="Cookie 必须是 Cookie-Editor 导出的 JSON 数组")
    data = [c for c in data if isinstance(c, dict) and c.get("name")]
    if not data:
        raise HTTPException(status_code=400, detail="没有有效 Cookie 条目")
    write_json(COOKIE_PATH, data)
    try:
        COOKIE_PATH.chmod(0o600)
    except OSError:
        pass
    return {"ok": True, "count": len(data)}


@app.post("/api/run")
async def api_run(payload: dict, request: Request):
    require_auth(request)
    if STATE["running"]:
        raise HTTPException(status_code=409, detail="已有任务在跑")
    kind = "dry-run" if payload.get("dry_run") else "send"
    mode = str(payload.get("mode") or "auto")
    if mode not in {"auto", "report", "video", "config"}:
        mode = "auto"
    ok, _ = cookie_ok()
    if not ok:
        raise HTTPException(status_code=400, detail="还没配置 Cookie")
    only = str(payload.get("only") or "").strip()
    asyncio.create_task(run_task(kind, mode, only))
    return {"ok": True, "kind": kind, "mode": mode, "only": only}


@app.post("/api/city")
async def api_set_city(payload: dict, request: Request):
    """单独改某个好友的城市（前端输入后自动保存，不用点"保存配置"）。"""
    require_auth(request)
    name = str(payload.get("name", "")).strip()
    city = str(payload.get("city", "") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少好友名")
    cfg = read_json(CONFIG_PATH, {})
    hit = False
    for t in cfg.get("targets") or []:
        if str(t.get("name")) == name:
            t["city"] = city or None
            hit = True
    if not hit:
        raise HTTPException(status_code=404, detail=f"配置里没有好友「{name}」，先保存一次配置")
    write_json(CONFIG_PATH, cfg)
    return {"ok": True, "name": name, "city": city or None}


@app.get("/api/report/preview")
async def api_report_preview(request: Request, name: str | None = None, city: str | None = None):
    """预览今日晨报文本（真实拉新闻/天气，不发送）。不传 name 就用第一个好友（含其城市）。"""
    require_auth(request)
    cfg = read_json(CONFIG_PATH, {})
    rep_cfg = cfg.get("daily_report") or {}
    targets = cfg.get("targets") or []
    if not name:
        if not targets:
            raise HTTPException(status_code=400, detail="还没配置好友")
        name = targets[0].get("name") or "好友"
        if city is None:
            city = targets[0].get("city")
    elif city is None:
        # 传了好友名但没传城市：从配置里取该好友的城市（否则预览会缺天气段）
        for t in targets:
            if str(t.get("name")) == name:
                city = t.get("city")
                break
    news = await fetch_news(int(rep_cfg.get("news_count", 5) or 5))
    text = await build_for_target(name, city, news, rep_cfg)
    return {
        "ok": True,
        "text": text,
        "news": news,
        "city": city,
        "warning": None if news else "热点新闻接口暂时不可用，文本里会少「今日热点」段",
    }


@app.get("/api/runlog")
async def api_runlog(request: Request, lines: int = 300):
    require_auth(request)
    log = STATE["log"] or "（本次进程内还没有运行记录）"
    return {
        "running": STATE["running"],
        "kind": STATE["kind"],
        "returncode": STATE["returncode"],
        "log": "\n".join(log.splitlines()[-lines:]),
        "file_log": tail_file(sorted(ARTIFACTS.glob("cron-*.log"))[-1], 200) if list(ARTIFACTS.glob("cron-*.log")) else "",
        "result": read_json(ARTIFACTS / "result.json", None),
    }


@app.post("/api/scan")
async def api_scan(request: Request):
    """扫描私信会话列表：返回昵称 + 火花天数（不发送任何消息）。"""
    require_auth(request)
    ok, _ = cookie_ok()
    if not ok:
        raise HTTPException(status_code=400, detail="还没配置 Cookie")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(BASE / "tools" / "scan_conversations.py"),
        cwd=str(BASE),
        env=child_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"扫描失败：{text[-1500:]}")
    try:
        items = json.loads(text.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"扫描输出解析失败：{text[-1500:]}") from exc
    return {"ok": True, "items": items}


@app.get("/api/videos/pool")
async def api_videos_pool(request: Request, refresh: int = 0):
    """抓「我的喜欢 + 热点榜」并按类型分类，返回每类数量与样例（给网页预览用）。30 分钟内复用缓存。"""
    require_auth(request)
    ok, _ = cookie_ok()
    if not ok:
        raise HTTPException(status_code=400, detail="还没配置 Cookie")
    now = time.time()
    if not refresh and POOL_CACHE["data"] and now - POOL_CACHE["at"] < 1800:
        return {**POOL_CACHE["data"], "cached": True}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(BASE / "tools" / "fetch_pool.py"),
        "--all" if refresh == 2 else "",
        cwd=str(BASE),
        env=child_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"拉取视频池失败：{text[-1500:]}")
    try:
        data = json.loads(text.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"视频池输出解析失败：{text[-1500:]}") from exc
    POOL_CACHE["data"] = data
    POOL_CACHE["at"] = now
    return data


@app.get("/api/hot")
async def api_hot(request: Request, refresh: int = 0):
    """拉取抖音热点榜视频（给"视频分享"用），10 分钟内复用缓存。"""
    require_auth(request)
    ok, _ = cookie_ok()
    if not ok:
        raise HTTPException(status_code=400, detail="还没配置 Cookie")
    now = time.time()
    if not refresh and HOT_CACHE["items"] and now - HOT_CACHE["at"] < 600:
        return {"ok": True, "items": HOT_CACHE["items"], "cached": True}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(BASE / "tools" / "fetch_hot.py"),
        cwd=str(BASE),
        env=child_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"拉取热点榜失败：{text[-1200:]}")
    try:
        items = json.loads(text.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"热点榜输出解析失败：{text[-1200:]}") from exc
    HOT_CACHE.update(items=items, at=now)
    return {"ok": True, "items": items, "cached": False}


@app.get("/api/logs")
async def api_logs(request: Request, lines: int = 200):
    require_auth(request)
    files = sorted(ARTIFACTS.glob("*.log"))
    if not files:
        return {"file": None, "text": "（暂无日志）"}
    return {"file": files[-1].name, "text": tail_file(files[-1], lines)}


def tail_file(path: Path, lines: int) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except Exception as exc:  # noqa: BLE001
        return f"读取失败: {exc}"



# ---------------------------------------------------------------- 扫码登录
QR: dict = {"id": None, "status": "idle", "error": "", "image": None, "task": None}


def _qr_data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def _capture_qr(page) -> bytes:
    """截取登录弹窗里的二维码（优先按元素裁，兜底按固定区域裁）"""
    try:
        for sel in ('img[src^="data:image"]', '[class*="login"] img', 'img'):
            loc = page.locator(sel).first
            if not await loc.count():
                continue
            box = await loc.bounding_box()
            if box and 150 <= box.get("width", 0) <= 420 and 150 <= box.get("height", 0) <= 420:
                ratio = box["width"] / max(1, box["height"])
                if not (0.7 <= ratio <= 1.4):
                    continue
                pad = 10
                clip = {
                    "x": max(0, box["x"] - pad),
                    "y": max(0, box["y"] - pad),
                    "width": box["width"] + pad * 2,
                    "height": box["height"] + pad * 2,
                }
                return await page.screenshot(clip=clip, type="png")
    except Exception:  # noqa: BLE001
        pass
    try:
        return await page.screenshot(clip={"x": 410, "y": 380, "width": 250, "height": 250}, type="png")
    except Exception:  # noqa: BLE001
        return await page.screenshot(type="png")


async def _qr_login_worker(qid: str) -> None:
    from playwright.async_api import async_playwright

    pw = None
    browser = None
    context = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = await context.new_page()
        await page.goto("https://www.douyin.com/", wait_until="load", timeout=60000)
        await asyncio.sleep(6)
        for text in ("登录", "扫码登录"):
            try:
                loc = page.get_by_text(text, exact=True).first
                if await loc.count():
                    await loc.click(timeout=5000)
                    await asyncio.sleep(1.5)
            except Exception:  # noqa: BLE001
                pass
        png = await _capture_qr(page)
        QR["image"] = _qr_data_url(png)
        QR["status"] = "waiting"
        QR["rev"] = 1
        try:
            (ARTIFACTS / "qr-debug.png").write_bytes(await page.screenshot(type="png"))
        except Exception:  # noqa: BLE001
            pass
        last_cap = time.time()
        deadline = time.time() + 300
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                login_btn = page.get_by_text("登录", exact=True)
                visible = await login_btn.count() and await login_btn.first.is_visible()
            except Exception:  # noqa: BLE001
                visible = True
            if not visible:
                # 登录成功：保存档案 + cookie
                secrets_dir = COOKIE_PATH.parent
                await context.storage_state(path=str(secrets_dir / "storage_state.json"))
                live = await context.cookies()
                keep = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
                rows = [{k: c.get(k) for k in keep if c.get(k) is not None} for c in live if c.get("name")]
                write_json(COOKIE_PATH, rows)
                (secrets_dir / "storage_state.json").chmod(0o600)
                COOKIE_PATH.chmod(0o600)
                QR["status"] = "ok"
                return
            # 每 15 秒重新截一次（抖音二维码会自动刷新，避免用户拿到已失效的码）
            if time.time() - last_cap > 15:
                try:
                    for txt in ("点击刷新", "刷新二维码", "重新获取", "刷新"):
                        loc = page.get_by_text(txt, exact=False).first
                        if await loc.count() and await loc.is_visible():
                            await loc.click(timeout=3000)
                            await asyncio.sleep(1)
                            break
                except Exception:  # noqa: BLE001
                    pass
                try:
                    png = await _capture_qr(page)
                    QR["image"] = _qr_data_url(png)
                    QR["rev"] = int(QR.get("rev") or 0) + 1
                    last_cap = time.time()
                except Exception:  # noqa: BLE001
                    pass
        QR["status"] = "expired"
        QR["error"] = "二维码已过期，请重新生成"
    except Exception as exc:  # noqa: BLE001
        QR["status"] = "failed"
        QR["error"] = str(exc)[:300]
    finally:
        try:
            if context:
                await context.close()
            if browser:
                await browser.close()
            if pw:
                await pw.stop()
        except Exception:  # noqa: BLE001
            pass


@app.post("/api/login/qr/start")
async def api_login_qr_start(request: Request):
    require_auth(request)
    if QR["status"] in ("waiting", "starting") and QR.get("task"):
        return {"ok": True, "id": QR["id"], "image": QR.get("image")}
    QR.update(id=secrets.token_hex(6), status="starting", error="", image=None, rev=0)
    QR["task"] = asyncio.create_task(_qr_login_worker(QR["id"]))
    for _ in range(60):
        if QR.get("image"):
            break
        await asyncio.sleep(0.5)
    if not QR.get("image"):
        return {"ok": False, "error": QR.get("error") or "生成二维码超时", "id": QR["id"]}
    return {"ok": True, "id": QR["id"], "image": QR["image"], "rev": QR.get("rev", 1)}


@app.get("/api/login/qr/status")
async def api_login_qr_status(request: Request, id: str = "", since: int = 0):
    require_auth(request)
    if id and QR.get("id") and id != QR["id"]:
        return {"status": "expired", "error": "旧二维码已失效"}
    rev = int(QR.get("rev") or 0)
    out = {"status": QR["status"], "error": QR.get("error") or "", "id": QR.get("id"), "rev": rev}
    if rev > since and QR.get("image"):
        out["image"] = QR["image"]
    return out

LOGIN_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>登录 · 抖音续火花配置台</title>
<style>body{background:#161823;color:#eee;font-family:system-ui,-apple-system,"PingFang SC",sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#1f2130;padding:32px 36px;border-radius:14px;box-shadow:0 8px 40px #0008}
h1{font-size:18px;margin:0 0 18px}input{width:220px;padding:10px 12px;border-radius:8px;border:1px solid #3a3d52;
background:#12131b;color:#eee}button{margin-top:14px;width:100%;padding:10px;border:0;border-radius:8px;
background:#fe2c55;color:#fff;font-size:15px;cursor:pointer}__ERR__</style>
<form method="post" onsubmit="return doLogin(event)">
<h1>🔥 抖音续火花 · 配置台</h1><input id="pw" type="password" placeholder="访问密码" autofocus>
<button type="submit">进入</button></form>
<script>async function doLogin(e){e.preventDefault();
const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({password:document.getElementById('pw').value})});
if(r.ok)location.reload();else alert('密码错误');return false}</script></html>"""
