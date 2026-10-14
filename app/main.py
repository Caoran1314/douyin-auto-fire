from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import hashlib
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.browser import AuthenticationError, RiskControlError, SearchBoxNotReadyError, open_douyin, open_private_messages, save_trace, verify_login
from app.config import ConfigError, load_settings, load_task
from app.daily_report import build_for_target, build_report, fetch_news, pick_quote
from app.douyin import DouyinChat, PageOperationError
from app.history import AlreadyRunningError, History, run_lock
from app.models import Message, Settings, TargetResult
from app.notifier import send_dingtalk_notification
from app.privacy import RedactingFormatter, build_target_aliases, redact_text, target_alias
from app.sender import send_message
from app import video_sources
from app.video_share import pick_videos, share_video_to_friend


LOGGER = logging.getLogger("douyin_sender")

# 运行模式：
#   auto   当天第一次=晨报，之后=只转发视频（默认）
#   report 强制发晨报
#   video  只转发视频
#   config 按 config.json 里每个好友的 messages 发（旧行为）
MODE_AUTO = "auto"
MODE_REPORT = "report"
MODE_VIDEO = "video"
MODE_CONFIG = "config"
MODES = (MODE_AUTO, MODE_REPORT, MODE_VIDEO, MODE_CONFIG)


async def run(dry_run: bool = False, env_file: str | None = None, mode: str = MODE_AUTO) -> int:
    settings = load_settings(env_file)
    task = load_task(settings)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    aliases = build_target_aliases(task.targets)
    _configure_logging(settings.artifacts_dir, aliases)

    if not settings.storage_state and not settings.cookie:
        raise ConfigError("必须配置 DOUYIN_STORAGE_STATE 或 DOUYIN_COOKIE")

    history = History(settings.artifacts_dir / "history.json")
    run_date = history.run_date(task.timezone)
    raw = _raw_config(settings)
    resolved_mode = _resolve_mode(mode, settings, run_date)
    LOGGER.info("本次模式: %s（请求 %s，当天是否已发晨报=%s）", resolved_mode, mode, _report_sent_today(settings, run_date))

    # 每个好友本轮要发的消息：晨报模式=一条晨报文本；视频模式=只转发视频；config 模式=按配置
    message_map: dict[str, tuple[Message, ...]] = {}
    if resolved_mode == MODE_REPORT:
        report_cfg = raw.get("daily_report") or {}
        news = await fetch_news(int(report_cfg.get("news_count", 5) or 5))
        if not news:
            LOGGER.warning("热点新闻拉取失败，晨报将只含天气与名言")
        LOGGER.info("晨报：抓到 %d 条热点", len(news))
        reports = await asyncio.gather(
            *[
                build_for_target(
                    t.name,
                    t.city,
                    news,
                    {**report_cfg, "greeting": report_cfg.get("greeting", f"早上好")},
                )
                for t in task.targets
            ],
            return_exceptions=True,
        )
        for target, rep in zip(task.targets, reports):
            if isinstance(rep, Exception):
                LOGGER.warning("生成 %s 的晨报失败：%s", target.name, rep)
                rep = build_report(target.name, news, None, pick_quote())
            message_map[target.name] = (Message(type="text", content=rep),)
            if dry_run:
                LOGGER.info("[dry-run] %s 的晨报预览：\n%s", target.name, rep)
    elif resolved_mode == MODE_VIDEO:
        per_friend = int((raw.get("video_share") or {}).get("per_friend", 1) or 1)
        for t in task.targets:
            message_map[t.name] = tuple(Message(type="video_share") for _ in range(max(1, per_friend)))
    else:
        for t in task.targets:
            message_map[t.name] = t.messages

    results: list[TargetResult] = []
    screenshots: list[Path] = []
    fatal_error: Exception | None = None
    try:
        async with open_douyin(settings) as session:
            page = session.page
            trace_saved = False
            try:
                await open_private_messages(page)
            except Exception as exc:
                LOGGER.exception("打开抖音私信页面失败")
                screenshot = await _screenshot(page, settings.artifacts_dir, "login")
                if screenshot:
                    screenshots.append(screenshot)
                if settings.trace and not trace_saved:
                    try:
                        await save_trace(session, _trace_path(settings.artifacts_dir))
                        trace_saved = True
                    except Exception:
                        LOGGER.exception("保存 trace 失败")
                label = "登录检查" if isinstance(exc, (AuthenticationError, RiskControlError)) else "运行检查"
                results.append(TargetResult(target=label, status="failed", error=str(exc)))
                fatal_error = exc

            if fatal_error is None:
                chat = DouyinChat(page, timeout_ms=int(task.target_open_timeout_seconds * 1000))
                pool = VideoPool(
                    categories=video_categories(raw),
                    custom=video_urls(raw),
                    exclude=video_excludes(raw),
                )
                for index, target in enumerate(task.targets):
                    sent = 0
                    alias = target_alias(index)
                    try:
                        LOGGER.info("处理好友: %s", alias)
                        msgs = message_map.get(target.name, ())
                        need_chat = any(_resolve_message(m).type != "video_share" for m in msgs)
                        # 文字消息必须开聊天页；纯视频转发不需要（分享面板自己搜得到人）
                        if need_chat:
                            await _ensure_chat_page(page)
                            chat_ready, chat_error = await _open_chat(chat, target.name, task, alias)
                        else:
                            chat_ready, chat_error = False, None
                        for message_index, message in enumerate(message_map.get(target.name, ())):
                            resolved = _resolve_message(message)
                            # dry-run 只演练"视频分享"这条新链路，文字消息保持原来的跳过行为
                            if dry_run and resolved.type != "video_share":
                                continue
                            message_id = _message_id(message_index, message)
                            key = history.key(task.task_id, run_date, target.name, message_id)
                            if not dry_run and task.prevent_duplicates and history.contains(key):
                                LOGGER.info(
                                    "跳过当天已处理或结果不确定的消息: %s #%d",
                                    alias,
                                    message_index + 1,
                                )
                                continue
                            if resolved.type == "video_share":
                                # 个别视频的分享浮层打不开：换下一条视频重试（最多 3 次），
                                # 每次失败都先把页面恢复回私信页，避免把后续好友全带崩
                                last_exc: Exception | None = None
                                for attempt in range(1, 4):
                                    video = await pool.next(page, preferred=resolved.url if attempt == 1 else None)
                                    cat = video.get("category") or "热点榜"
                                    try:
                                        if dry_run:
                                            await share_video_to_friend(page, video["url"], target.name, mark_only=True)
                                            LOGGER.info("dry-run：视频分享链路可用 [#%s] %s -> %s", cat, video["url"], alias)
                                        else:
                                            await share_video_to_friend(page, video["url"], target.name)
                                            LOGGER.info("已分享视频 [#%s] %s -> %s", cat, video["url"], alias)
                                            if task.prevent_duplicates:
                                                history.mark_success(key)
                                            sent += 1
                                        last_exc = None
                                        break
                                    except Exception as exc:  # noqa: BLE001
                                        last_exc = exc
                                        LOGGER.warning(
                                            "第 %d/3 次分享失败（[#%s] %s）：%s", attempt, cat, video["url"], exc
                                        )
                                        await _ensure_chat_page(page, force=True)
                                if last_exc is not None:
                                    # 有些人不在「分享给朋友」列表里（非互关/陌生人），视频转发必然失败。
                                    # 这种情况退回发一条文字，保证火花不断（可用 video_fallback_text=false 关掉）
                                    fallback = None
                                    if (raw.get("video_share") or {}).get("fallback_text", True):
                                        fallback = _fallback_text_message(raw, target)
                                    if fallback is None:
                                        raise last_exc
                                    if dry_run:
                                        # 演练模式绝不能真发：只记录"正式运行时会改发文字"
                                        LOGGER.info("dry-run：%s 无法转发视频，正式运行时将改为发文字续火花", alias)
                                    else:
                                        LOGGER.warning(
                                            "%s 无法转发视频（%s），改为发文字续火花", alias, str(last_exc)[:60]
                                        )
                                        await _ensure_chat_page(page, force=True)
                                        opened, _ = await _open_chat(chat, target.name, task, alias)
                                        if not opened:
                                            raise last_exc
                                        await verify_login(page, timeout_ms=3_000)
                                        await send_message(page, chat, fallback, task.stickers)
                                        if task.prevent_duplicates:
                                            history.mark_success(key)
                                        sent += 1
                                # 分享会跳离私信页；还有后续消息才需要重开该好友
                                await _ensure_chat_page(page, force=True)
                                if message_index < len(msgs) - 1:
                                    chat_ready, chat_error = await _open_chat(chat, target.name, task, alias)
                            else:
                                if not chat_ready:
                                    chat_ready, chat_error = await _open_chat(chat, target.name, task, alias)
                                    if not chat_ready:
                                        raise chat_error or PageOperationError("打开聊天失败")
                                if task.prevent_duplicates:
                                    history.reserve(key)
                                await verify_login(page, timeout_ms=3_000)
                                await send_message(page, chat, resolved, task.stickers)
                                if task.prevent_duplicates:
                                    history.mark_success(key)
                                sent += 1
                            if message_index < len(target.messages) - 1 and not dry_run:
                                await asyncio.sleep(random.uniform(task.interval_min, task.interval_max))
                        results.append(TargetResult(target=target.name, status="success", sent=sent, target_alias=alias))
                    except (AuthenticationError, RiskControlError) as exc:
                        LOGGER.exception("处理好友时登录状态失效: %s", alias)
                        screenshot = await _screenshot(page, settings.artifacts_dir, alias)
                        if screenshot:
                            screenshots.append(screenshot)
                        if settings.trace and not trace_saved:
                            try:
                                await save_trace(session, _trace_path(settings.artifacts_dir))
                                trace_saved = True
                            except Exception:
                                LOGGER.exception("保存 trace 失败")
                        results.append(TargetResult(target=target.name, status="failed", sent=sent, error=str(exc), target_alias=alias))
                        fatal_error = exc
                        break
                    except Exception as exc:
                        LOGGER.exception("好友处理失败: %s", alias)
                        screenshot = await _screenshot(page, settings.artifacts_dir, alias)
                        if screenshot:
                            screenshots.append(screenshot)
                        if settings.trace and not trace_saved:
                            try:
                                await save_trace(session, _trace_path(settings.artifacts_dir))
                                trace_saved = True
                            except Exception:
                                LOGGER.exception("保存 trace 失败")
                        results.append(TargetResult(target=target.name, status="failed", sent=sent, error=str(exc), target_alias=alias))
                        if not task.continue_on_error:
                            break
                    if index < len(task.targets) - 1 and not dry_run:
                        await asyncio.sleep(random.uniform(task.interval_min, task.interval_max))

            if settings.trace and not trace_saved:
                try:
                    await session.context.tracing.stop()
                except Exception as exc:
                    LOGGER.exception("停止 trace 失败")
                    if fatal_error is None:
                        fatal_error = exc
                        results.append(TargetResult(target="运行收尾", status="failed", error=str(exc)))
    except Exception as exc:
        if fatal_error is None:
            fatal_error = exc
            results.append(TargetResult(target="运行检查", status="failed", error=str(exc)))

    _write_results(settings.artifacts_dir, task.task_id, dry_run, results, aliases)
    await _notify_dingtalk(settings, task.task_id, dry_run, results, screenshots)
    succeeded = sum(result.status == "success" for result in results)
    failed = sum(result.status == "failed" for result in results)
    # 晨报发成功过就记一笔：当天之后的运行（含手动）改成只转发视频
    if not dry_run and resolved_mode == MODE_REPORT and fatal_error is None and succeeded:
        _mark_report_sent(settings, run_date, task.task_id)
        LOGGER.info("已记录：%s 的晨报已发送，今天后续运行将只转发视频", run_date)
    LOGGER.info("执行结束: 成功 %d，失败 %d", succeeded, failed)
    if fatal_error is not None:
        raise fatal_error
    return 1 if failed else 0


def main() -> int:
    args = _parse_cli_args()
    try:
        settings = load_settings(args.env_file)
        with run_lock(settings.artifacts_dir / "run.lock"):
            return asyncio.run(run(dry_run=args.dry_run, env_file=args.env_file, mode=args.mode))
    except (ConfigError, AuthenticationError, RiskControlError, SearchBoxNotReadyError, AlreadyRunningError) as exc:
        print(f"错误: {exc}")
        return 2
    except KeyboardInterrupt:
        print("任务已取消")
        return 130


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="向多个抖音好友发送配置的消息")
    parser.add_argument("--dry-run", action="store_true", help="只验证登录和好友，不发送消息")
    parser.add_argument("--env-file", help="指定 .env 文件路径")
    parser.add_argument(
        "--mode",
        choices=list(MODES),
        default=MODE_AUTO,
        help="auto=当天首次发晨报、之后只转发视频（默认）；report=强制晨报；video=只转发视频；config=按配置发",
    )
    return parser.parse_args()


def _configure_logging(
    artifacts_dir: Path,
    aliases: dict[str, str] | None = None,
    *,
    label: str | None = None,
    reset: bool = False,
) -> None:
    if reset or not LOGGER.handlers:
        for handler in list(LOGGER.handlers):
            LOGGER.removeHandler(handler)
            if isinstance(handler, logging.FileHandler):
                handler.close()
        LOGGER.setLevel(logging.INFO)
        pattern = "%(asctime)s %(levelname)s %(message)s"
        if label:
            pattern = pattern.replace(" %(message)s", f" [{label}] %(message)s")
        formatter = RedactingFormatter(pattern, aliases=aliases)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(artifacts_dir / "run.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
        LOGGER.addHandler(stream_handler)
        return
    # 已有 handler（多账号模式下 run() 内部会再次调用）：只更新脱敏别名。
    for handler in LOGGER.handlers:
        if isinstance(handler.formatter, RedactingFormatter):
            handler.formatter.aliases = dict(aliases or {})


async def _screenshot(page, artifacts_dir: Path, label: str) -> Path | None:
    safe_label = re.sub(r"[^A-Za-z0-9_.\-一-鿿]+", "_", label).strip("_")
    suffix = hashlib.sha1(label.encode("utf-8")).hexdigest()[:6]
    safe_label = f"{safe_label}-{suffix}" if safe_label else f"failure-{suffix}"
    directory = artifacts_dir / "screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now():%Y%m%d-%H%M%S}-{safe_label}.png"
    try:
        await page.screenshot(path=path, full_page=True)
        return path
    except Exception:
        LOGGER.exception("保存截图失败")
        return None


def _write_results(
    artifacts_dir: Path,
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    aliases: dict[str, str] | None = None,
) -> None:
    payload = {
        "task_id": task_id,
        "dry_run": dry_run,
        "finished_at": datetime.now().astimezone().isoformat(),
        "results": [_redacted_result(result, aliases) for result in results],
    }
    (artifacts_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _redacted_result(result: TargetResult, aliases: dict[str, str] | None = None) -> dict:
    aliases = dict(aliases or {})
    aliases[result.target] = result.target_alias or aliases.get(result.target, result.target)
    return {
        "target": aliases[result.target],
        "status": result.status,
        "sent": result.sent,
        "error": redact_text(result.error, aliases) if result.error else None,
    }


async def _notify_dingtalk(
    settings: Settings,
    task_id: str,
    dry_run: bool,
    results: list[TargetResult],
    screenshots: list[Path],
) -> None:
    if not settings.dingtalk_webhook or not settings.dingtalk_secret:
        return
    try:
        await send_dingtalk_notification(
            settings.dingtalk_webhook,
            settings.dingtalk_secret,
            task_id,
            dry_run,
            results,
            screenshots,
        )
        LOGGER.info("钉钉通知发送成功")
    except Exception:
        LOGGER.exception("钉钉通知发送失败，不影响本次任务结果")


def _trace_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / "traces" / f"{datetime.now():%Y%m%d-%H%M%S}.zip"


def _message_id(index, message) -> str:
    payload = json.dumps(asdict(message), ensure_ascii=False, sort_keys=True, default=str)
    return f"{index}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def _resolve_message(message: Message) -> Message:
    """把 random 链解析成具体消息（random 不支持嵌套，最多一层）。"""
    while message.type == "random":
        message = random.choice(message.choices)
    return message


def _fallback_text_message(raw: dict, target) -> Message | None:
    """视频转发失败时的兜底文字：优先用全局消息池，其次用该好友自己的文字消息。"""
    pool = [str(m).strip() for m in (raw.get("message_pool") or []) if str(m).strip()]
    if pool:
        return Message(type="text", content=random.choice(pool))
    for message in target.messages:
        resolved = _resolve_message(message)
        if resolved.type == "text" and resolved.content:
            return resolved
    return None


async def _open_chat(chat, name: str, task, alias: str) -> tuple[bool, Exception | None]:
    """打开某个好友的会话；失败不抛给上层（视频转发不依赖它，见 _open_chat 的调用点）。"""
    try:
        await chat.open_target(name, retries=task.target_open_retries)
        return True, None
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("%s 会话打不开（%s）——文字消息会跳过，视频转发不受影响", alias, str(exc)[:60])
        return False, exc


async def _ensure_chat_page(page, force: bool = False) -> None:
    """确保当前停在私信页。

    视频分享、扫描、热点榜等操作会把页面导航到别的地址；任何一步失败后如果不切回来，
    后续所有好友都会在错误的页面上找搜索框 → 全部报"搜索不到目标好友"（实测踩过）。
    force=True 时即使已在私信页也重新打开（分享完需要清掉浮层/重载列表）。
    """
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        url = ""
    if force or "/chat" not in url:
        if not force:
            LOGGER.info("当前不在私信页（%s），先切回", url[:70])
        try:
            await open_private_messages(page)
        except Exception:  # noqa: BLE001
            LOGGER.exception("切回私信页失败，稍后重试")
            await page.wait_for_timeout(2_000)
            await open_private_messages(page)


def _raw_config(settings: Settings) -> dict:
    try:
        return json.loads(Path(settings.task_config_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _marker_file(settings: Settings) -> Path:
    return settings.artifacts_dir / "last_run.json"


def _read_marker(settings: Settings) -> dict:
    try:
        return json.loads(_marker_file(settings).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _report_sent_today(settings: Settings, run_date: str) -> bool:
    return _read_marker(settings).get("report_date") == run_date


def _mark_report_sent(settings: Settings, run_date: str, task_id: str) -> None:
    try:
        _marker_file(settings).write_text(
            json.dumps(
                {"report_date": run_date, "task_id": task_id, "at": datetime.now().astimezone().isoformat()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception("写入 last_run.json 失败")


def _resolve_mode(requested: str, settings: Settings, run_date: str) -> str:
    """auto：当天还没发过晨报 → 发晨报；已经发过 → 只转发视频。"""
    if requested in (MODE_REPORT, MODE_VIDEO, MODE_CONFIG):
        return requested
    return MODE_VIDEO if _report_sent_today(settings, run_date) else MODE_REPORT


def video_urls(raw: dict) -> list[str]:
    vs = raw.get("video_share") or {}
    if vs.get("source") == "list":
        return [u for u in (vs.get("urls") or []) if isinstance(u, str) and u.strip()]
    return []


def video_excludes(raw: dict) -> list[str]:
    vs = raw.get("video_share") or {}
    return [u for u in (vs.get("exclude") or []) if isinstance(u, str)]


def video_categories(raw: dict) -> dict:
    vs = raw.get("video_share") or {}
    return video_sources.normalize_categories(vs.get("categories"))


class VideoPool:
    """按类型随机取视频。

    分类池来自「我的喜欢 + 抖音热点榜」，按标题关键词分类（网页可配要发哪几类、关键词可改）。
    每次取视频：随机挑一个还有存货的类型 → 从该类型里取一条；某类型空了就跳过；
    全空则回退到任意未发过的视频；仍然没有就抛错。
    """

    def __init__(
        self,
        categories: dict | None = None,
        custom: list[str] | None = None,
        exclude: list[str] | None = None,
        likes_scrolls: int = 5,
    ) -> None:
        self._categories = video_sources.normalize_categories(categories)
        self._custom = [u for u in (custom or []) if u]
        self._exclude = {u for u in (exclude or []) if u}
        self._likes_scrolls = likes_scrolls
        self._pools: dict[str, list[dict[str, str]]] = {}
        self._used: set[str] = set()
        self._loaded = False

    async def _load(self, page) -> None:
        if self._custom:
            self._pools = {
                "指定链接": [{"url": u, "title": "指定视频"} for u in self._custom if u not in self._exclude]
            }
        else:
            self._pools = await video_sources.build_pool(
                page,
                self._categories,
                exclude=list(self._exclude),
                likes_scrolls=self._likes_scrolls,
            )
        self._loaded = True
        LOGGER.info(
            "视频池就绪：%s（共 %d 条）",
            {k: len(v) for k, v in self._pools.items()},
            sum(len(v) for v in self._pools.values()),
        )

    def _free(self, name: str) -> list[dict[str, str]]:
        return [v for v in self._pools.get(name, []) if v["url"] not in self._used]

    def _take(self, name: str) -> dict[str, str]:
        item = self._free(name)[0]
        self._used.add(item["url"])
        return {**item, "category": name}

    async def next(self, page, preferred: str | None = None) -> dict[str, str]:
        if preferred:
            return {"url": preferred, "title": "指定视频", "category": "指定"}
        if not self._loaded:
            await self._load(page)

        names = [n for n in self._categories if self._free(n)]
        if names:
            random.shuffle(names)
            return self._take(names[0])

        # 该类都空了：先看别的类型有没有存货，再重新抓一次
        others = [n for n in self._pools if self._free(n)]
        if others:
            random.shuffle(others)
            return self._take(others[0])

        if not self._custom:
            self._pools = {}
            self._loaded = False
            await self._load(page)
            names = [n for n in self._pools if self._free(n)]
            if names:
                return self._take(names[0])

        raise RuntimeError("没有可用的视频（视频池为空：我的喜欢/热点榜都没抓到，或手动链接池为空）")
