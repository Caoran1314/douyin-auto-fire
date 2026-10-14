"""每日晨报内容：热点新闻（60秒读懂世界）+ 天气（Open-Meteo）+ 名言（本地词库）。

设计原则：
- 新闻/天气走公开免密钥接口，全部有超时与降级；任一环节失败都不影响其他段落。
- 名言用本地词库按日期取，不依赖网络（接口挂了也有内容）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import urllib.parse
import urllib.request
from datetime import date, datetime

LOGGER = logging.getLogger("douyin_sender")

NEWS_API = "https://60s.viki.moe/v2/60s"
GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 12

# WMO weather code → 中文
WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨",
    56: "冻毛毛雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}

WEATHER_EMOJI = {
    "晴": "☀️", "晴间多云": "🌤", "多云": "⛅", "阴": "☁️", "有雾": "🌫", "雾凇": "🌫",
    "毛毛雨": "🌦", "小雨": "🌧", "中雨": "🌧", "大雨": "⛈", "阵雨": "🌦", "中阵雨": "🌧", "强阵雨": "⛈",
    "冻毛毛雨": "🌧", "冻雨": "🌧", "强冻雨": "🌧",
    "小雪": "🌨", "中雪": "🌨", "大雪": "❄️", "米雪": "🌨", "阵雪": "🌨", "强阵雪": "❄️",
    "雷阵雨": "⛈", "雷阵雨伴冰雹": "⛈", "强雷暴伴冰雹": "⛈",
}

QUOTES = [
    "人生天地之间，若白驹之过隙，忽然而已。—— 庄子",
    "千里之行，始于足下。—— 老子",
    "不积跬步，无以至千里。—— 荀子",
    "路虽远，行则将至；事虽难，做则必成。—— 《荀子》",
    "宝剑锋从磨砺出，梅花香自苦寒来。—— 《警世贤文》",
    "天行健，君子以自强不息。—— 《周易》",
    "纸上得来终觉浅，绝知此事要躬行。—— 陆游",
    "长风破浪会有时，直挂云帆济沧海。—— 李白",
    "会当凌绝顶，一览众山小。—— 杜甫",
    "莫愁前路无知己，天下谁人不识君。—— 高适",
    "星光不问赶路人，时光不负有心人。—— 佚名",
    "你所热爱的，就是你的生活。—— 佚名",
    "世界以痛吻我，要我报之以歌。—— 泰戈尔",
    "生活在别处，而你所站之处，即是远方。—— 米兰·昆德拉",
    "生活不止眼前的苟且，还有诗和远方的田野。—— 高晓松",
    "每一个不曾起舞的日子，都是对生命的辜负。—— 尼采",
    "人的一切痛苦，本质上都是对自己无能的愤怒。—— 王小波",
    "你要做一个不动声色的大人了。—— 村上春树",
    "愿你以渺小启程，以伟大结束。—— 佚名",
    "心之所向，素履以往；生如逆旅，一苇以航。—— 《小窗幽记》",
    "不要因为走得太远，而忘记为什么出发。—— 纪伯伦",
    "既然选择了远方，便只顾风雨兼程。—— 汪国真",
    "行动是治愈恐惧的良药。—— 威廉·詹姆斯",
    "慢慢来，一切都来得及。—— 佚名",
    "凡是过往，皆为序章。—— 莎士比亚",
    "把时间花在进步上，而不是抱怨上。—— 佚名",
    "愿你走出半生，归来仍是少年。—— 佚名",
    "岁月不居，时节如流，唯热爱可抵岁月漫长。—— 佚名",
    "不必太张扬，是花自然香。—— 佚名",
    "所有的努力，都会绕个大圈回到你身上。—— 佚名",
]

BLANK = "\n \n"  # 抖音输入框会把连续空行折叠，用「空格行」保住空行


# ------------------------------------------------------------------ 新闻
def _http_json(url: str, timeout: int = TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "douyin-auto-fire/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace"))


async def fetch_news(limit: int = 5) -> list[str]:
    """每日热点新闻。主源：60秒读懂世界；失败自动降级为空列表（调用方再兜底）。"""
    try:
        data = await asyncio.to_thread(_http_json, NEWS_API)
        news = ((data or {}).get("data") or {}).get("news") or []
        cleaned = [str(n).strip() for n in news if str(n).strip()]
        if cleaned:
            LOGGER.info("新闻源取到 %d 条", len(cleaned))
            return cleaned[:limit]
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("新闻接口失败：%s", exc)
    return []


async def fetch_tip() -> str | None:
    """60秒接口自带的每日一句（可作名言兜底）。"""
    try:
        data = await asyncio.to_thread(_http_json, NEWS_API)
        tip = ((data or {}).get("data") or {}).get("tip")
        return str(tip).strip() if tip else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 天气
async def fetch_weather(city: str) -> dict | None:
    """按城市名查天气（Open-Meteo，免密钥）。返回 {city, desc, emoji, cur, lo, hi}。"""
    city = (city or "").strip()
    if not city:
        return None
    try:
        geo = await asyncio.to_thread(
            _http_json, f"{GEO_API}?name={urllib.parse.quote(city)}&count=1&language=zh&format=json"
        )
        results = (geo or {}).get("results") or []
        if not results:
            LOGGER.warning("城市「%s」没查到坐标", city)
            return None
        r = results[0]
        lat, lon = r["latitude"], r["longitude"]
        name = r.get("name") or city
        admin = r.get("admin1") or ""
        fc = await asyncio.to_thread(
            _http_json,
            f"{FORECAST_API}?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,weather_code"
            "&daily=temperature_2m_max,temperature_2m_min,weather_code"
            "&timezone=Asia%2FShanghai&forecast_days=1",
        )
        cur = (fc.get("current") or {})
        daily = (fc.get("daily") or {})
        code = int((cur.get("weather_code") if cur.get("weather_code") is not None
                    else (daily.get("weather_code") or [0])[0]))
        desc = WMO.get(code, "未知")
        return {
            "city": name,
            "admin": admin,
            "desc": desc,
            "emoji": WEATHER_EMOJI.get(desc, "🌡"),
            "cur": cur.get("temperature_2m"),
            "lo": (daily.get("temperature_2m_min") or [None])[0],
            "hi": (daily.get("temperature_2m_max") or [None])[0],
        }
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("查天气失败(%s)：%s", city, exc)
        return None


# ------------------------------------------------------------------ 名言
def pick_quote(seed: str | None = None) -> str:
    """按日期取名言（同一天所有人同一句，稳定可复现）。"""
    key = seed or date.today().isoformat()
    rng = random.Random(key)
    return rng.choice(QUOTES)


# ------------------------------------------------------------------ 组装
def build_report(
    name: str,
    news: list[str],
    weather: dict | None = None,
    quote: str | None = None,
    greeting: str = "早上好",
) -> str:
    """拼装晨报文本。结构：早上好 <昵称> / 【今日热点】/ 【天气】/ 「名言」"""
    parts: list[str] = []
    head = f"{greeting} {name}".strip()
    if weather and weather.get("emoji"):
        head = f"{head} {weather['emoji']}"
    parts.append(head)

    if news:
        lines = [f"{i + 1}. {t}" for i, t in enumerate(news)]
        parts.append("【今日热点】" + "\n" + "\n".join(lines))

    if weather:
        loc = weather.get("city") or ""
        seg = f"{weather.get('desc', '')}"
        temps = ""
        if weather.get("lo") is not None and weather.get("hi") is not None:
            temps = f"，{round(weather['lo'])}~{round(weather['hi'])}℃"
        if weather.get("cur") is not None:
            temps += f"（现在 {round(weather['cur'])}℃）"
        parts.append(f"【天气】{loc}：{seg}{temps}")

    if quote:
        parts.append(f"「{quote}」")

    return BLANK.join(parts)


async def build_for_target(name: str, city: str | None, news: list[str], cfg: dict | None = None) -> str:
    """按配置为某个好友生成晨报文本。"""
    cfg = cfg or {}
    weather = await fetch_weather(city) if (city and cfg.get("include_weather", True)) else None
    quote = pick_quote() if cfg.get("include_quote", True) else None
    return build_report(name, news, weather, quote, cfg.get("greeting", "早上好"))


async def fetch_all(limit: int = 5) -> dict:
    """一次性抓齐新闻 + 名言（天气按人各自查）。"""
    news = await fetch_news(limit)
    tip = None
    if not news:
        tip = await fetch_tip()
    return {"news": news, "tip": tip, "quote": pick_quote(), "date": datetime.now().strftime("%Y-%m-%d")}
