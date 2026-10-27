"""Independent life plans with stable crontab semantics and durable run claims."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from .store import AlbumError, now, text_value, uid

FORMAT = "companion-life-plans-v1"
WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
WEEKDAY_CN = ("周日", "周一", "周二", "周三", "周四", "周五", "周六")
PHOTO_MODES = ("auto", "always", "never")
DAY_MODES = (
    "cron",
    "school_day",
    "holiday_rest",
    "sleep_before_work",
    "night_owl_before_rest",
)
TRIP_MODES = ("trip", "holiday", "school")
MAX_WINDOW = 120
MAX_PENDING_ONCE = 5

DEFAULT_HOLIDAYS = {
    "2026-01-01": "元旦",
    "2026-01-02": "元旦",
    "2026-01-03": "元旦",
    "2026-02-15": "春节",
    "2026-02-16": "春节",
    "2026-02-17": "春节",
    "2026-02-18": "春节",
    "2026-02-19": "春节",
    "2026-02-20": "春节",
    "2026-02-21": "春节",
    "2026-02-22": "春节",
    "2026-02-23": "春节",
    "2026-04-04": "清明节",
    "2026-04-05": "清明节",
    "2026-04-06": "清明节",
    "2026-04-18": "广西壮族三月三",
    "2026-04-19": "广西壮族三月三",
    "2026-04-20": "广西壮族三月三",
    "2026-04-21": "广西壮族三月三",
    "2026-05-01": "劳动节",
    "2026-05-02": "劳动节",
    "2026-05-03": "劳动节",
    "2026-05-04": "劳动节",
    "2026-05-05": "劳动节",
    "2026-06-19": "端午节",
    "2026-06-20": "端午节",
    "2026-06-21": "端午节",
    "2026-09-25": "中秋节",
    "2026-09-26": "中秋节",
    "2026-09-27": "中秋节",
    "2026-10-01": "国庆节",
    "2026-10-02": "国庆节",
    "2026-10-03": "国庆节",
    "2026-10-04": "国庆节",
    "2026-10-05": "国庆节",
    "2026-10-06": "国庆节",
    "2026-10-07": "国庆节",
    "2027-01-01": "元旦",
    "2027-01-02": "元旦",
    "2027-01-03": "元旦",
    "2027-02-05": "春节",
    "2027-02-06": "春节",
    "2027-02-07": "春节",
    "2027-02-08": "春节",
    "2027-02-09": "春节",
    "2027-02-10": "春节",
    "2027-02-11": "春节",
    "2027-04-03": "清明节",
    "2027-04-04": "清明节",
    "2027-04-05": "清明节",
    "2027-04-09": "广西壮族三月三",
    "2027-04-10": "广西壮族三月三",
    "2027-04-11": "广西壮族三月三",
    "2027-05-01": "劳动节",
    "2027-05-02": "劳动节",
    "2027-05-03": "劳动节",
    "2027-05-04": "劳动节",
    "2027-05-05": "劳动节",
    "2027-06-08": "端午节",
    "2027-06-09": "端午节",
    "2027-06-10": "端午节",
    "2027-09-15": "中秋节",
    "2027-09-16": "中秋节",
    "2027-09-17": "中秋节",
    "2027-10-01": "国庆节",
    "2027-10-02": "国庆节",
    "2027-10-03": "国庆节",
    "2027-10-04": "国庆节",
    "2027-10-05": "国庆节",
    "2027-10-06": "国庆节",
    "2027-10-07": "国庆节",
}

DEFAULT_WORKDAYS = {
    "2026-01-04": "元旦调休补班",
    "2026-02-14": "春节前调休补班",
    "2026-02-28": "春节后调休补班",
    "2026-04-26": "劳动节前调休补班",
    "2026-05-09": "劳动节后调休补班",
    "2026-09-20": "中秋节前调休补班",
    "2026-10-10": "国庆节后调休补班",
    "2027-01-31": "春节前调休补班",
    "2027-02-13": "春节后调休补班",
    "2027-04-25": "劳动节前调休补班",
    "2027-05-08": "劳动节后调休补班",
    "2027-09-26": "国庆节前调休补班",
    "2027-10-09": "国庆节后调休补班",
}

DEFAULT_CUSTOM_DAYS = [
    {
        "date": "01-01",
        "name": "元旦",
        "is_holiday": True,
        "note": "新年的第一天，适合甜甜蜜蜜向哥哥讨新年祝福",
    },
    {
        "date": "02-14",
        "name": "情人节",
        "is_holiday": False,
        "note": "浪漫的情人节，傲娇又期待哥哥的礼物与陪伴",
    },
    {
        "date": "03-08",
        "name": "女生节/女神节",
        "is_holiday": False,
        "note": "大学里热闹的女生节，可以撒娇让哥哥宠小狐狸",
    },
    {
        "date": "农历03-03",
        "name": "广西壮族三月三",
        "is_holiday": True,
        "note": "广西特色三月三假期，放假不用上课，有五色糯米饭与热闹节日氛围",
    },
    {
        "date": "05-20",
        "name": "520表白日",
        "is_holiday": False,
        "note": "谐音我爱你，嘴硬害羞但心里超期待哥哥表白",
    },
    {
        "date": "06-01",
        "name": "儿童节",
        "is_holiday": False,
        "note": "理直气壮宣称小狐狸还是宝宝，要哥哥给买零食或礼物",
    },
    {
        "date": "农历07-07",
        "name": "七夕节",
        "is_holiday": False,
        "note": "传统七夕情人节，满心期待和哥哥约会贴贴",
    },
    {
        "date": "09-10",
        "name": "教师节",
        "is_holiday": False,
        "note": "校园里给老师送祝福的日子",
    },
    {
        "date": "10-31",
        "name": "万圣节夜",
        "is_holiday": False,
        "note": "不给糖就捣蛋，适合摇着狐狸耳朵和尾巴恶作剧",
    },
    {
        "date": "12-21",
        "name": "冬至",
        "is_holiday": False,
        "note": "冬至大如年，提醒哥哥吃饺子或汤圆、天冷钻进狐狸尾巴取暖",
    },
    {
        "date": "12-24",
        "name": "平安夜",
        "is_holiday": False,
        "note": "温暖的冬夜与苹果，想和哥哥依偎在一起",
    },
    {
        "date": "12-25",
        "name": "圣诞节",
        "is_holiday": False,
        "note": "浪漫冬日节日氛围，讨要圣诞抱抱",
    },
    {
        "date": "12-31",
        "name": "跨年夜",
        "is_holiday": False,
        "note": "一年最后一夜，想和哥哥一起跨年倒数迎接零点",
    },
]

DEFAULT_SCHOOL_TERMS = {
    "winter_start": "01-18",
    "winter_end": "02-25",
    "summer_start": "07-08",
    "summer_end": "08-31",
    "exam_ranges": "01-04~01-17,06-22~07-07",
}

LUNAR_INFO = (
    0x04BD8,
    0x04AE0,
    0x0A570,
    0x054D5,
    0x0D260,
    0x0D950,
    0x16554,
    0x056A0,
    0x09AD0,
    0x055D2,
    0x04AE0,
    0x0A5B6,
    0x0A4D0,
    0x0D250,
    0x1D255,
    0x0B540,
    0x0D6A0,
    0x0ADA2,
    0x095B0,
    0x14977,
    0x04970,
    0x0A4B0,
    0x0B4B5,
    0x06A50,
    0x06D40,
    0x1AB54,
    0x02B60,
    0x09570,
    0x052F2,
    0x04970,
    0x06566,
    0x0D4A0,
    0x0EA50,
    0x06E95,
    0x05AD0,
    0x02B60,
    0x186E3,
    0x092E0,
    0x1C8D7,
    0x0C950,
    0x0D4A0,
    0x1D8A6,
    0x0B550,
    0x056A0,
    0x1A5B4,
    0x025D0,
    0x092D0,
    0x0D2B2,
    0x0A950,
    0x0B557,
    0x06CA0,
    0x0B550,
    0x15355,
    0x04DA0,
    0x0A5B0,
    0x14573,
    0x052B0,
    0x0A9A8,
    0x0E950,
    0x06AA0,
    0x0AEA6,
    0x0AB50,
    0x04B60,
    0x0AAE4,
    0x0A570,
    0x05260,
    0x0F263,
    0x0D950,
    0x05B57,
    0x056A0,
    0x096D0,
    0x04DD5,
    0x04AD0,
    0x0A4D0,
    0x0D4D4,
    0x0D250,
    0x0D558,
    0x0B540,
    0x0B6A0,
    0x195A6,
    0x095B0,
    0x049B0,
    0x0A974,
    0x0A4B0,
    0x0B27A,
    0x06A50,
    0x06D40,
    0x0AF46,
    0x0AB60,
    0x09570,
    0x04AF5,
    0x04970,
    0x064B0,
    0x074A3,
    0x0EA50,
    0x06B58,
    0x05AC0,
    0x0AB60,
    0x096D5,
    0x092E0,
    0x0C960,
    0x0D954,
    0x0D4A0,
    0x0DA50,
    0x07552,
    0x056A0,
    0x0ABB7,
    0x025D0,
    0x092D0,
    0x0CAB5,
    0x0A950,
    0x0B4A0,
    0x0BAA4,
    0x0AD50,
    0x055D9,
    0x04BA0,
    0x0A5B0,
    0x15176,
    0x052B0,
    0x0A930,
    0x07954,
    0x06AA0,
    0x0AD50,
    0x05B52,
    0x04B60,
    0x0A6E6,
    0x0A4E0,
    0x0D260,
    0x0EA65,
    0x0D530,
    0x05AA0,
    0x076A3,
    0x096D0,
    0x04AFB,
    0x04AD0,
    0x0A4D0,
    0x1D0B6,
    0x0D250,
    0x0D520,
    0x0DD45,
    0x0B5A0,
    0x056D0,
    0x055B2,
    0x049B0,
    0x0A577,
    0x0A4B0,
    0x0AA50,
    0x1B255,
    0x06D20,
    0x0ADA0,
    0x14B63,
    0x09370,
    0x049F8,
    0x04970,
    0x064B0,
    0x168A6,
    0x0EA50,
    0x06AA0,
    0x1A6C4,
    0x0AAE0,
    0x092E0,
    0x0D2E3,
    0x0C960,
    0x0D557,
    0x0D4A0,
    0x0DA50,
    0x05D55,
    0x056A0,
    0x0A6D0,
    0x055D4,
    0x052D0,
    0x0A9B8,
    0x0A950,
    0x0B4A0,
    0x0B6A6,
    0x0AD50,
    0x055A0,
    0x0ABA4,
    0x0A5B0,
    0x052B0,
    0x0B273,
    0x06930,
    0x07337,
    0x06AA0,
    0x0AD50,
    0x14B55,
    0x04B60,
    0x0A570,
    0x054E4,
    0x0D160,
    0x0E968,
    0x0D520,
    0x0DAA0,
    0x16AA6,
    0x056D0,
    0x04AE0,
    0x0A9D4,
    0x0A2D0,
    0x0D150,
    0x0F252,
    0x0D520,
)


LUNAR_MONTH_CN = (
    "",
    "正月",
    "二月",
    "三月",
    "四月",
    "五月",
    "六月",
    "七月",
    "八月",
    "九月",
    "十月",
    "冬月",
    "腊月",
)
LUNAR_DAY_CN = (
    "",
    "初一",
    "初二",
    "初三",
    "初四",
    "初五",
    "初六",
    "初七",
    "初八",
    "初九",
    "初十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
    "十七",
    "十八",
    "十九",
    "二十",
    "廿一",
    "廿二",
    "廿三",
    "廿四",
    "廿五",
    "廿六",
    "廿七",
    "廿八",
    "廿九",
    "三十",
)


@functools.lru_cache(maxsize=512)
def solar_to_lunar_mmdd(d: date) -> str:
    """Return 'lunar:MM-DD' for a solar date between 1900 and 2100."""
    offset = (d - date(1900, 1, 31)).days
    if offset < 0:
        return ""
    for idx, info in enumerate(LUNAR_INFO):
        leap = info & 0xF
        days = sum(30 if (info & (0x10000 >> m)) else 29 for m in range(1, 13))
        if leap:
            days += 30 if (info & 0x10000) else 29
        if offset < days:
            year_info = info
            break
        offset -= days
    else:
        return ""
    leap = year_info & 0xF
    for m in range(1, 13):
        mdays = 30 if (year_info & (0x10000 >> m)) else 29
        if offset < mdays:
            return f"lunar:{m:02d}-{offset + 1:02d}"
        offset -= mdays
        if leap == m:
            ldays = 30 if (year_info & 0x10000) else 29
            if offset < ldays:
                return f"lunar:{m:02d}-{offset + 1:02d}"
            offset -= ldays
    return ""


def solar_to_lunar_cn(d: date) -> str:
    """Return full Chinese lunar date such as '农历三月初三' or '农历九月十八'."""
    raw = solar_to_lunar_mmdd(d)
    if not raw or ":" not in raw:
        return ""
    mm, dd = map(int, raw.split(":", 1)[1].split("-"))
    if 1 <= mm <= 12 and 1 <= dd <= 30:
        return f"农历{LUNAR_MONTH_CN[mm]}{LUNAR_DAY_CN[dd]}"
    return f"农历{mm:02d}-{dd:02d}"


def normalize_custom_date_rule(rule: str) -> str:
    """Normalize custom day rule to 'MM-DD', 'YYYY-MM-DD', or '农历MM-DD'."""
    s = str(rule or "").strip()
    if not s:
        return ""
    if s.lower().startswith("lunar:"):
        s = "农历" + s[6:].strip()
    elif s.startswith("农历:"):
        s = "农历" + s[3:].strip()
    if s.startswith("农历"):
        tail = s[2:].strip()
        if re.fullmatch(r"\d{2}-\d{2}", tail):
            return f"农历{tail}"
        for m_idx in range(1, 13):
            m_cn = LUNAR_MONTH_CN[m_idx]
            if tail.startswith(m_cn):
                d_part = tail[len(m_cn) :].strip()
                if d_part in LUNAR_DAY_CN[1:]:
                    d_idx = LUNAR_DAY_CN.index(d_part)
                    return f"农历{m_idx:02d}-{d_idx:02d}"
        return ""
    if re.fullmatch(r"(?:\d{4}-)?\d{2}-\d{2}", s):
        return s
    return ""


def expand_holiday_entries(raw_entries) -> dict[str, str]:
    """Expand single dates ('YYYY-MM-DD') and date ranges ('YYYY-MM-DD~YYYY-MM-DD') into a daily map."""
    out: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    if isinstance(raw_entries, dict):
        pairs = [(str(k).strip(), str(v).strip()) for k, v in raw_entries.items()]
    elif isinstance(raw_entries, list):
        for line in raw_entries:
            s = str(line or "").strip()
            if "=" in s:
                k, v = s.split("=", 1)
                pairs.append((k.strip(), v.strip()))
    elif isinstance(raw_entries, str):
        for line in raw_entries.splitlines():
            s = line.strip()
            if "=" in s:
                k, v = s.split("=", 1)
                pairs.append((k.strip(), v.strip()))
    for k, v in pairs:
        if not k or not v:
            continue
        label = v[:80]
        if "~" in k:
            s_str, e_str = [x.strip() for x in k.split("~", 1)]
            if (
                re.fullmatch(r"\d{4}-\d{2}-\d{2}", s_str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", e_str)
                and s_str <= e_str
            ):
                try:
                    cur = date.fromisoformat(s_str)
                    end = date.fromisoformat(e_str)
                    if (end - cur).days <= 60:
                        while cur <= end:
                            out[cur.isoformat()] = label
                            cur += timedelta(days=1)
                except ValueError:
                    pass
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", k):
            try:
                date.fromisoformat(k)
                out[k] = label
            except ValueError:
                pass
    return dict(sorted(out.items()))


def collapse_holiday_ranges(date_map: dict[str, str]) -> list[str]:
    """Collapse consecutive dates with identical names into 'YYYY-MM-DD~YYYY-MM-DD=Name' lines."""
    if not isinstance(date_map, dict) or not date_map:
        return []
    valid: list[tuple[date, str]] = []
    for k, v in sorted(date_map.items()):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(k)) and str(v).strip():
            try:
                valid.append((date.fromisoformat(str(k)), str(v).strip()))
            except ValueError:
                pass
    if not valid:
        return []
    lines: list[str] = []
    start_d, end_d, cur_name = valid[0][0], valid[0][0], valid[0][1]
    for d, name in valid[1:]:
        if name == cur_name and d == end_d + timedelta(days=1):
            end_d = d
        else:
            if start_d == end_d:
                lines.append(f"{start_d.isoformat()}={cur_name}")
            else:
                lines.append(f"{start_d.isoformat()}~{end_d.isoformat()}={cur_name}")
            start_d, end_d, cur_name = d, d, name
    if start_d == end_d:
        lines.append(f"{start_d.isoformat()}={cur_name}")
    else:
        lines.append(f"{start_d.isoformat()}~{end_d.isoformat()}={cur_name}")
    return lines


DEFAULT_SEARCH_SCHEMES = [
    {
        "id": "default_astrbot",
        "name": "复用 AstrBot 全局配置",
        "api_url": "https://api.tavily.com/search",
        "api_key": "",
        "is_builtin": True,
    }
]


def _in_mmdd_range(d: date, start: str, end: str) -> bool:
    if not start or not end:
        return False
    if len(start) == 10 or len(end) == 10:
        try:
            return date.fromisoformat(start) <= d <= date.fromisoformat(end)
        except ValueError:
            return False
    mmdd = d.isoformat()[5:]
    if start <= end:
        return start <= mmdd <= end
    return mmdd >= start or mmdd <= end


def normalize_calendar(
    raw: dict | None = None, *, prune_before: date | None = None, strict: bool = False
) -> dict:
    """Return a validated calendar configuration merged with defaults and auto-pruned of expired years/trips."""
    raw = raw if isinstance(raw, dict) else {}
    holidays = dict(DEFAULT_HOLIDAYS)
    if "holidays" in raw and raw["holidays"] is not None:
        holidays = expand_holiday_entries(raw["holidays"])
    workdays = dict(DEFAULT_WORKDAYS)
    if "workdays" in raw and raw["workdays"] is not None:
        workdays = expand_holiday_entries(raw["workdays"])
    if prune_before is None:
        try:
            ts = now()
            if ts > 1700000000:
                prune_before = datetime.fromtimestamp(
                    ts, ZoneInfo("Asia/Shanghai")
                ).date()
        except Exception:
            prune_before = None
    if prune_before is not None:
        min_year = prune_before.year
        recent_cutoff = (prune_before - timedelta(days=60)).isoformat()
        holidays = {
            k: v
            for k, v in holidays.items()
            if int(k[:4]) >= min_year or k >= recent_cutoff
        }
        workdays = {
            k: v
            for k, v in workdays.items()
            if int(k[:4]) >= min_year or k >= recent_cutoff
        }
    custom_days = [dict(x) for x in DEFAULT_CUSTOM_DAYS]
    if isinstance(raw.get("custom_days"), list):
        custom_days = []
        for item in raw["custom_days"][:100]:
            if not isinstance(item, dict):
                continue
            rule = normalize_custom_date_rule(item.get("date") or "")
            name = str(item.get("name") or "").strip()[:60]
            if not rule or not name:
                continue
            custom_days.append(
                {
                    "date": rule,
                    "name": name,
                    "is_holiday": bool(item.get("is_holiday", False)),
                    "note": str(item.get("note") or "").strip()[:240],
                }
            )
    terms = dict(DEFAULT_SCHOOL_TERMS)
    if isinstance(raw.get("school_terms"), dict):
        for k in (
            "winter_start",
            "winter_end",
            "summer_start",
            "summer_end",
            "exam_ranges",
        ):
            val = raw["school_terms"].get(k)
            if isinstance(val, str):
                terms[k] = val.strip()[:120]
            elif k == "exam_ranges" and isinstance(val, list):
                parts = []
                for item in val[:10]:
                    if isinstance(item, dict) and item.get("start") and item.get("end"):
                        parts.append(f"{item['start']}~{item['end']}")
                    elif isinstance(item, str) and "~" in item:
                        parts.append(item.split("=")[0].strip())
                terms[k] = ",".join(parts)[:120]
    trips = []
    trip_cutoff = (
        (prune_before - timedelta(days=30)).isoformat() if prune_before else ""
    )
    if isinstance(raw.get("trips"), list):
        for item in raw["trips"][:50]:
            if not isinstance(item, dict):
                continue
            start_d = str(item.get("start_date") or "").strip()
            end_d = str(item.get("end_date") or "").strip()
            title = str(item.get("title") or "").strip()[:80]
            try:
                if (
                    not title
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_d)
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_d)
                    or date.fromisoformat(start_d) > date.fromisoformat(end_d)
                ):
                    raise ValueError
            except ValueError:
                if strict:
                    raise AlbumError(
                        "行程起止日期无效，请使用真实的 YYYY-MM-DD 日期"
                    ) from None
                continue
            if trip_cutoff and end_d < trip_cutoff:
                continue
            wake_t = str(item.get("wake_time") or "").strip()
            if wake_t and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", wake_t):
                wake_t = ""
            try:
                wake_window = max(0, min(int(item.get("wake_window", 15)), 60))
            except (TypeError, ValueError):
                wake_window = 15
            trips.append(
                {
                    "id": str(item.get("id") or uid("trip_"))[:60],
                    "scope": str(item.get("scope") or "").strip()[:300],
                    "title": title,
                    "start_date": start_d,
                    "end_date": end_d,
                    "wake_time": wake_t,
                    "wake_window": wake_window,
                    "mode": item.get("mode")
                    if item.get("mode") in TRIP_MODES
                    else "trip",
                    "note": str(item.get("note") or "").strip()[:300],
                }
            )
    search_schemes = [dict(DEFAULT_SEARCH_SCHEMES[0])]
    if isinstance(raw.get("search_schemes"), list):
        for item in raw["search_schemes"][:20]:
            if not isinstance(item, dict):
                continue
            s_id = str(item.get("id") or "").strip()
            if not s_id or s_id == "default_astrbot":
                continue
            s_name = str(item.get("name") or "").strip()[:60]
            s_url = str(item.get("api_url") or "").strip()[:300]
            s_key = str(item.get("api_key") or "").strip()[:2000]
            search_schemes.append(
                {
                    "id": s_id,
                    "name": s_name or "自定义方案",
                    "api_url": s_url or "https://api.tavily.com/search",
                    "api_key": s_key,
                    "is_builtin": False,
                }
            )
    active_search_scheme = "default_astrbot"
    raw_active = str(raw.get("active_search_scheme") or "").strip()
    if any(s["id"] == raw_active for s in search_schemes):
        active_search_scheme = raw_active

    personal = {}
    if isinstance(raw.get("personal_calendars"), dict):
        for scope, entry in raw["personal_calendars"].items():
            if not isinstance(scope, str) or not scope or not isinstance(entry, dict):
                continue
            normalized = normalize_calendar(
                {k: entry[k] for k in ("school_terms", "custom_days") if k in entry},
                prune_before=prune_before,
                strict=strict,
            )
            personal[scope] = {
                k: normalized[k] for k in ("school_terms", "custom_days")
            }

    return {
        "holidays": holidays,
        "workdays": workdays,
        "holiday_ranges": collapse_holiday_ranges(holidays),
        "workday_ranges": collapse_holiday_ranges(workdays),
        "custom_days": custom_days,
        "school_terms": terms,
        "trips": trips,
        "search_schemes": search_schemes,
        "active_search_scheme": active_search_scheme,
        "personal_calendars": personal,
    }


def _basic_day_flags(d: date, cal: dict) -> dict:
    iso = d.isoformat()
    mmdd = iso[5:]
    lunar_mmdd = solar_to_lunar_mmdd(d)
    lunar_rule = f"农历{lunar_mmdd[6:]}" if lunar_mmdd else ""
    lunar_cn = solar_to_lunar_cn(d)
    holidays = cal.get("holidays", DEFAULT_HOLIDAYS)
    workdays = cal.get("workdays", DEFAULT_WORKDAYS)
    custom_days = cal.get("custom_days", DEFAULT_CUSTOM_DAYS)
    terms = cal.get("school_terms", DEFAULT_SCHOOL_TERMS)

    statutory_name = holidays.get(iso, "")
    workday_name = workdays.get(iso, "")
    matched_custom = [
        item
        for item in custom_days
        if normalize_custom_date_rule(item.get("date") or "") in (iso, mmdd, lunar_rule)
        or item.get("date") in (iso, mmdd, lunar_mmdd, lunar_rule, lunar_cn)
    ]
    custom_holiday_names = [c["name"] for c in matched_custom if c.get("is_holiday")]
    is_official_holiday = bool(statutory_name or custom_holiday_names)
    is_makeup_workday = bool(workday_name) and not is_official_holiday

    vacation = ""
    if _in_mmdd_range(d, terms.get("winter_start", ""), terms.get("winter_end", "")):
        vacation = "寒假"
    elif _in_mmdd_range(d, terms.get("summer_start", ""), terms.get("summer_end", "")):
        vacation = "暑假"

    is_exam = False
    for part in (terms.get("exam_ranges") or "").split(","):
        if "~" in part:
            s_part, e_part = [x.strip() for x in part.split("~", 1)]
            if _in_mmdd_range(d, s_part, e_part):
                is_exam = True
                break

    is_weekend = d.weekday() >= 5
    yaozhi_rest = (
        is_official_holiday
        or (bool(vacation) and not is_makeup_workday)
        or (is_weekend and not is_makeup_workday)
    )
    brother_rest = is_official_holiday or (is_weekend and not is_makeup_workday)
    festival_names = []
    if statutory_name:
        festival_names.append(statutory_name)
    for c in matched_custom:
        if c["name"] not in festival_names:
            festival_names.append(c["name"])
    return {
        "date": iso,
        "mmdd": mmdd,
        "lunar": lunar_cn,
        "lunar_cn": lunar_cn,
        "lunar_rule": lunar_rule,
        "lunar_mmdd": lunar_mmdd,
        "weekday": d.weekday(),
        "weekday_cn": WEEKDAY_CN[(d.weekday() + 1) % 7],
        "is_weekend": is_weekend,
        "statutory_holiday": statutory_name,
        "workday_name": workday_name,
        "festival_names": festival_names,
        "custom_notes": [
            f"{c['name']}：{c['note']}" for c in matched_custom if c.get("note")
        ],
        "is_official_holiday": is_official_holiday,
        "is_makeup_workday": is_makeup_workday,
        "vacation": vacation,
        "is_exam_week": is_exam,
        "is_rest_day": yaozhi_rest,
        "is_school_day": not yaozhi_rest,
        "brother_rest": brother_rest,
    }


def day_profile(d: date, calendar: dict | None = None, scope: str = "") -> dict:
    """Compute full day & 4-state weekend classification, active trip, and prompt notes."""
    cal = normalize_calendar(calendar)
    if scope:
        cal.update(cal["personal_calendars"].get(scope, {}))
    cur = _basic_day_flags(d, cal)
    prev_d = _basic_day_flags(d - timedelta(days=1), cal)
    next_d = _basic_day_flags(d + timedelta(days=1), cal)

    iso = cur["date"]
    active_trip = None
    for trip in cal.get("trips", []):
        if trip["start_date"] <= iso <= trip["end_date"] and (
            not trip.get("scope") or not scope or trip["scope"] == scope
        ):
            active_trip = dict(trip)
            start_dt = date.fromisoformat(trip["start_date"])
            end_dt = date.fromisoformat(trip["end_date"])
            active_trip["day_index"] = (d - start_dt).days + 1
            active_trip["total_days"] = (end_dt - start_dt).days + 1
            break

    if active_trip:
        if active_trip.get("mode") in ("trip", "holiday"):
            cur["is_rest_day"] = True
            cur["is_school_day"] = False
        elif active_trip.get("mode") == "school":
            cur["is_rest_day"] = False
            cur["is_school_day"] = True

    weekend_kind = ""
    if cur["is_weekend"]:
        other_weekend = prev_d if d.weekday() == 6 else next_d
        if cur["is_makeup_workday"]:
            weekend_kind = "makeup_workday"
            day_kind = "makeup_workday"
            label = f"调休补班/补课周末（{cur['workday_name']}）"
            mood_note = (
                f"今天虽是{cur['weekday_cn']}，却是法定调休补班/补课日（{cur['workday_name']}），"
                "原本的周末懒觉已自动切换为上课/工作日作息；语气里可自然流露周末还要早起补课、以及心疼对方调休上班的小怨念与撒娇打气。"
            )
        elif (
            cur["is_official_holiday"]
            or prev_d["is_official_holiday"]
            or next_d["is_official_holiday"]
        ):
            weekend_kind = "holiday_weekend"
            day_kind = "holiday_weekend"
            fest = (
                "、".join(cur["festival_names"])
                or prev_d["statutory_holiday"]
                or next_d["statutory_holiday"]
                or "法定节假日"
            )
            label = f"法定长假连休周末（{fest}）"
            tomorrow_free = next_d["is_rest_day"] and next_d["brother_rest"]
            mood_note = (
                f"今天是「{fest}」长假连休期间的{cur['weekday_cn']}，充满浓厚的过节与长假狂欢感，适合聊出游、聚餐、追番或节日活动。"
                + (
                    "明天依然是假期不用早起上班/上课，完全没有普通周日晚上的收心焦虑！"
                    if d.weekday() == 6 and tomorrow_free
                    else ""
                )
            )
        elif other_weekend["is_makeup_workday"]:
            weekend_kind = "single_rest_weekend"
            day_kind = "single_rest_weekend"
            label = "调休单休周末"
            mood_note = (
                f"因为这个周末另一天要调休补班（{other_weekend['workday_name']}），今天（{cur['weekday_cn']}）是仅剩的一天宝贵单休日！"
                "语气里会格外心疼对方只休一天太辛苦，主张宅家躺平补觉、点好吃的或黏在一起温柔充电。"
            )
        elif cur["vacation"]:
            weekend_kind = "vacation_weekend"
            day_kind = "vacation_weekend"
            label = f"{cur['vacation']}周末"
            mood_note = (
                f"今天处于学校{cur['vacation']}期间的{cur['weekday_cn']}。"
                f"平时周一到周五只有你自己放{cur['vacation']}、对方还要上班；今天终于盼到周末对方也放假休息，可以全天黏在一起啦！"
            )
        else:
            weekend_kind = "normal_weekend"
            day_kind = "normal_weekend"
            label = "普通双休周末"
            if d.weekday() == 5:
                mood_note = "今天是平时学期的普通周六，双休充电开启，可以睡懒觉、逛吃、看番或悠闲度过。"
            else:
                mood_note = "今天是平时学期的普通周日，享受周末休闲之余，下午和傍晚可自然流露一点周末尾声感（明天周一又要早起上课/上班）。"
    else:
        if cur["is_official_holiday"]:
            day_kind = "official_holiday"
            fest = "、".join(cur["festival_names"]) or "节假日"
            label = f"节假日放假（{fest}）"
            mood_note = f"今天虽然是{cur['weekday_cn']}，但正逢「{fest}」放假不用上课/上班，白天的早课已自动静默并切换为假期休息作息，尽情享受假期氛围！"
        elif cur["vacation"]:
            day_kind = "school_vacation_weekday"
            label = f"{cur['vacation']}平日（自己放假/对方上班）"
            mood_note = (
                f"今天是学校{cur['vacation']}期间的{cur['weekday_cn']}，你自己放假不用去上课（白天按休息作息运行），"
                "但对方今天是工作日需要上班，可以在享受假期悠闲之余自然关心、投喂或坏心眼调侃正在上班的他。"
            )
        else:
            day_kind = "normal_workday"
            label = "平时上课/工作日" + ("·期末周" if cur["is_exam_week"] else "")
            mood_note = f"今天是平时的{cur['weekday_cn']}（上课/工作日）。" + (
                "当前处于学校期末复习/考试周阶段，可自然带有一点备考复习的生活感。"
                if cur["is_exam_week"]
                else ""
            )

    tomorrow_trip = None
    next_iso = next_d["date"]
    for trip in cal.get("trips", []):
        if trip["start_date"] <= next_iso <= trip["end_date"] and (
            not trip.get("scope") or not scope or trip["scope"] == scope
        ):
            tomorrow_trip = trip
            break

    cur_early_trip = bool(active_trip and active_trip.get("wake_time"))
    can_stay_up_predawn = bool(
        cur["is_rest_day"] and cur["brother_rest"] and not cur_early_trip
    )
    return {
        **cur,
        "day_kind": day_kind,
        "weekend_kind": weekend_kind,
        "label": label,
        "mood_note": mood_note,
        "active_trip": active_trip,
        "tomorrow_trip": tomorrow_trip,
        "can_stay_up_predawn": can_stay_up_predawn,
        "sleep_early_predawn": not can_stay_up_predawn,
        "tomorrow_is_rest": bool(next_d["is_rest_day"] and next_d["brother_rest"]),
        "tomorrow_label": next_d["statutory_holiday"]
        or next_d["workday_name"]
        or next_d["weekday_cn"],
    }


def jitter_offset(plan_id: str, anchor_ts: int, before: int, after: int) -> int:
    """Calculate a deterministic minute offset in [-before, +after] (returned in seconds)."""
    if before <= 0 and after <= 0:
        return 0
    span = before + after + 1
    digest = hashlib.sha256(f"{plan_id or 'preview'}:{anchor_ts}".encode()).digest()
    value = int.from_bytes(digest[:8], "big")
    delta_minutes = (value % span) - before
    return delta_minutes * 60


def _parse_weekday_names(weekday: str) -> set[str]:
    names = set()
    for part in weekday.split(","):
        span, *steps = part.split("/")
        step = int(steps[0]) if steps else 1
        if len(steps) > 1 or step < 1 or step > 7:
            raise ValueError
        if span == "*":
            start, end = 0, 6
        elif "-" in span:
            start, end = map(int, span.split("-"))
        else:
            start = int(span)
            end = 7 if steps else start
        if not 0 <= start <= end <= 7:
            raise ValueError
        names.update(WEEKDAYS[n % 7] for n in range(start, end + 1, step))
    return names


def triggers(expression: str, timezone: str) -> list[CronTrigger]:
    """Validate five-field POSIX crontab, including Sunday 0/7 and day OR semantics."""
    try:
        zone = ZoneInfo(timezone)
        minute, hour, day, month, weekday = expression.split()
        if not re.fullmatch(r"[\d*,/ -]+", expression):
            raise ValueError
        names = _parse_weekday_names(weekday)
        common = {"minute": minute, "hour": hour, "month": month, "timezone": zone}
        dow = ",".join(sorted(names))
        if day != "*" and weekday != "*":
            return [
                CronTrigger(**common, day=day),
                CronTrigger(**common, day_of_week=dow),
            ]
        return [CronTrigger(**common, day=day, day_of_week=dow)]
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise AlbumError(
            "时间规则须为五段数字 cron；星期 0/7=周日、1=周一。请使用有效 IANA 时区"
        ) from exc


def _is_morning_wake_slot(hour: int, minute: int) -> bool:
    return 6 <= hour < 11 or (hour == 11 and minute <= 30)


def _makeup_effective_dow(d: date, workday_name: str) -> str:
    mapping = {
        "周一": "mon",
        "星期一": "mon",
        "周二": "tue",
        "星期二": "tue",
        "周三": "wed",
        "星期三": "wed",
        "周四": "thu",
        "星期四": "thu",
        "周五": "fri",
        "星期五": "fri",
    }
    for k, v in mapping.items():
        if k in (workday_name or ""):
            return v
    return "mon" if d.weekday() == 5 else "fri"


def _daily_anchors_for_plan(
    plan: dict,
    d: date,
    zone: ZoneInfo,
    calendar: dict | None,
    override_wake_dates: set[str],
) -> list[dict]:
    """Return candidate anchor dicts on local date `d` for a routine plan under calendar & override rules."""
    day_mode = plan.get("day_mode") or "cron"
    scope = plan.get("scope") or ""
    prof = day_profile(d, calendar, scope)
    iso = d.isoformat()
    before = int(plan.get("window_before") or 0)
    after_win = int(plan.get("window_after") or 0)
    minute, hour, day_f, month_f, weekday_f = plan["cron"].split()

    if day_mode == "holiday_rest" and not prof["is_rest_day"]:
        return []
    if day_mode == "school_day":
        if not prof["is_school_day"]:
            return []
        allowed_dows = _parse_weekday_names(weekday_f)
        eff_dow = (
            WEEKDAYS[(d.weekday() + 1) % 7]
            if d.weekday() < 5
            else _makeup_effective_dow(d, prof["workday_name"])
        )
        if eff_dow not in allowed_dows:
            return []
    elif day_mode == "sleep_before_work":
        if not (prof["sleep_early_predawn"] or iso in override_wake_dates):
            return []
    elif day_mode == "night_owl_before_rest":
        if not (prof["can_stay_up_predawn"] and iso not in override_wake_dates):
            return []

    sched = (
        triggers(plan["cron"], zone.key)
        if day_mode == "cron"
        else triggers(f"{minute} {hour} {day_f} {month_f} *", zone.key)
    )
    start_of_day = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=zone)
    end_of_day = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=zone)
    cursor = start_of_day
    anchors = []
    trip = prof.get("active_trip")
    trip_wake = trip.get("wake_time", "") if trip else ""
    trip_shifted = False

    while cursor <= end_of_day:
        values = [t.get_next_fire_time(None, cursor) for t in sched]
        value = min((v for v in values if v is not None), default=None)
        if value is None or value > end_of_day:
            break
        if _is_morning_wake_slot(value.hour, value.minute):
            if iso in override_wake_dates:
                cursor = value + timedelta(seconds=1)
                continue
            if trip_wake:
                if day_mode == "holiday_rest" and not trip_shifted:
                    trip_shifted = True
                    hh, mm = map(int, trip_wake.split(":"))
                    wake_dt = datetime(d.year, d.month, d.day, hh, mm, 0, tzinfo=zone)
                    w_win = int(trip.get("wake_window", 15) or 15)
                    anchors.append(
                        {
                            "anchor_dt": wake_dt,
                            "anchor_ts": int(wake_dt.timestamp()),
                            "before": w_win,
                            "after": w_win,
                            "profile": prof,
                            "trip_wake": True,
                        }
                    )
                cursor = value + timedelta(seconds=1)
                continue
        anchors.append(
            {
                "anchor_dt": value,
                "anchor_ts": int(value.timestamp()),
                "before": before,
                "after": after_win,
                "profile": prof,
                "trip_wake": False,
            }
        )
        cursor = value + timedelta(seconds=1)
    return anchors


def next_schedule_details(
    plan: dict,
    after: datetime,
    count: int = 3,
    *,
    calendar: dict | None = None,
    override_wake_dates: set[str] | None = None,
) -> list[dict]:
    """Preview actual wall times and anchor offsets using the same triggers, calendar and jitter as execution."""
    timezone = plan.get("timezone") or "Asia/Shanghai"
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise AlbumError(
            "时间规则须为五段数字 cron；星期 0/7=周日、1=周一。请使用有效 IANA 时区"
        ) from exc
    before = int(plan.get("window_before") or 0)
    after_win = int(plan.get("window_after") or 0)
    plan_id = plan.get("id") or plan.get("name") or "preview"
    after_ts = int(after.timestamp())

    if plan.get("run_once"):
        anchor_ts = (int(plan.get("trigger_at") or 0) // 60) * 60
        if anchor_ts <= 0:
            return []
        offset = jitter_offset(plan_id, anchor_ts, before, after_win)
        actual_ts = anchor_ts + offset
        if actual_ts < after_ts:
            return []
        local_dt = datetime.fromtimestamp(actual_ts, zone)
        prof = day_profile(local_dt.date(), calendar, plan.get("scope", ""))
        return [
            {
                "actual": local_dt.isoformat(),
                "anchor": datetime.fromtimestamp(anchor_ts, zone).isoformat(),
                "delta_minutes": offset // 60,
                "actual_ts": actual_ts,
                "anchor_ts": anchor_ts,
                "day_label": prof["label"],
                "day_kind": prof["day_kind"],
            }
        ]

    triggers(plan["cron"], timezone)
    day_mode = plan.get("day_mode") or "cron"
    override_dates = override_wake_dates or set()
    if day_mode == "school_day" and not (
        _parse_weekday_names(plan["cron"].split()[4]) & set(WEEKDAYS[1:6])
    ):
        return []
    if day_mode == "cron":
        schedule = triggers(plan["cron"], timezone)
        candidates = []
        point = after - timedelta(minutes=after_win)
        while True:
            values = [t.get_next_fire_time(None, point) for t in schedule]
            value = min((v for v in values if v is not None), default=None)
            if value is None:
                break
            anchor_ts = int(value.timestamp())
            if len(candidates) >= count:
                candidates.sort(key=lambda item: (item["actual_ts"], item["anchor_ts"]))
                if anchor_ts - before * 60 > candidates[count - 1]["actual_ts"]:
                    break
            if _is_morning_wake_slot(value.hour, value.minute):
                prof = day_profile(value.date(), calendar, plan.get("scope", ""))
                trip = prof.get("active_trip")
                if value.date().isoformat() in override_dates or (
                    trip and trip.get("wake_time")
                ):
                    point = value + timedelta(seconds=1)
                    continue
            offset = jitter_offset(plan_id, anchor_ts, before, after_win)
            actual_ts = anchor_ts + offset
            if actual_ts >= after_ts:
                candidates.append(
                    {
                        "actual": datetime.fromtimestamp(actual_ts, zone).isoformat(),
                        "anchor": value.isoformat(),
                        "delta_minutes": offset // 60,
                        "actual_ts": actual_ts,
                        "anchor_ts": anchor_ts,
                    }
                )
            point = value + timedelta(seconds=1)
        candidates.sort(key=lambda item: (item["actual_ts"], item["anchor_ts"]))
        return candidates[:count]

    local_start = after.astimezone(zone).date() - timedelta(days=1)
    candidates = []
    minute, hour, day_field, month_field, _ = plan["cron"].split()
    date_schedule = triggers(f"{minute} {hour} {day_field} {month_field} *", timezone)
    cursor = datetime.combine(local_start, datetime.min.time(), zone)
    last_year = min(9999, after.year + max(40, count * 8))
    while cursor.year <= last_year:
        values = [t.get_next_fire_time(None, cursor) for t in date_schedule]
        value = min((v for v in values if v is not None), default=None)
        if value is None or value.year > last_year:
            break
        cur_date = value.date()
        for item in _daily_anchors_for_plan(
            plan, cur_date, zone, calendar, override_dates
        ):
            anchor_ts = item["anchor_ts"]
            offset = jitter_offset(plan_id, anchor_ts, item["before"], item["after"])
            actual_ts = anchor_ts + offset
            if actual_ts >= after_ts:
                prof = item["profile"]
                candidates.append(
                    {
                        "actual": datetime.fromtimestamp(actual_ts, zone).isoformat(),
                        "anchor": item["anchor_dt"].isoformat(),
                        "delta_minutes": offset // 60,
                        "actual_ts": actual_ts,
                        "anchor_ts": anchor_ts,
                        "day_label": (
                            f"出游早起覆写·{prof['active_trip']['title']}"
                            if item["trip_wake"] and prof.get("active_trip")
                            else prof["label"]
                        ),
                        "day_kind": prof["day_kind"],
                        "trip_wake": item["trip_wake"],
                    }
                )
        if len(candidates) >= count and (
            datetime.combine(
                cur_date + timedelta(days=1), datetime.min.time(), zone
            ).timestamp()
            - MAX_WINDOW * 60
            > sorted(c["actual_ts"] for c in candidates)[count - 1]
        ):
            break
        cursor = datetime.combine(
            cur_date + timedelta(days=1), datetime.min.time(), zone
        )
    candidates.sort(key=lambda item: (item["actual_ts"], item["anchor_ts"]))
    return candidates[:count]


def next_times(
    plan: dict,
    after: datetime,
    count: int = 3,
    *,
    calendar: dict | None = None,
    override_wake_dates: set[str] | None = None,
) -> list[str]:
    """Preview actual wall times using the same triggers as execution."""
    return [
        item["actual"]
        for item in next_schedule_details(
            plan,
            after,
            count,
            calendar=calendar,
            override_wake_dates=override_wake_dates,
        )
    ]


class LifePlans:
    def __init__(self, store, execute):
        self.store, self.execute = store, execute
        self.task = None
        self.workers = set()
        self.running_scopes = set()
        self.last_error = ""

    def get_calendar(self, scope: str = "") -> dict:
        """Read the persisted seasonal calendar & trips config, falling back to defaults."""
        row = self.store.db.execute(
            "SELECT data, revision FROM records WHERE kind='calendar' AND id='default'"
        ).fetchone()
        raw = json.loads(row["data"]) if row else {}
        cal = normalize_calendar(raw)
        if scope:
            cal.update(cal["personal_calendars"].get(scope, {}))
        cal["revision"] = int(row["revision"]) if row else 0
        return cal

    def save_calendar(
        self, data: dict, *, expected_revision: int | None = None, scope: str = ""
    ) -> dict:
        """Validate and persist calendar holidays, workdays, custom_days, school_terms and trips."""
        if not isinstance(data, dict):
            raise AlbumError("日历配置格式不正确")
        if isinstance(data.get("calendar"), dict):
            data = data["calendar"]
        with self.store.lock:
            current = self.get_calendar()
            if (
                expected_revision is not None
                and expected_revision != current["revision"]
            ):
                exc = AlbumError("日历已被修改，请刷新后重试")
                exc.status_code = 409
                raise exc
            merged = {
                "holidays": data.get("holidays", current["holidays"]),
                "workdays": data.get("workdays", current["workdays"]),
                "custom_days": data.get("custom_days", current["custom_days"]),
                "school_terms": data.get("school_terms", current["school_terms"]),
                "trips": data.get("trips", current["trips"]),
                "search_schemes": data.get(
                    "search_schemes", current.get("search_schemes")
                ),
                "active_search_scheme": data.get(
                    "active_search_scheme", current.get("active_search_scheme")
                ),
                "personal_calendars": current["personal_calendars"],
            }
            if scope:
                scoped = current["personal_calendars"].get(scope, current)
                merged["personal_calendars"] = {
                    **current["personal_calendars"],
                    scope: {
                        k: data.get(k, scoped[k])
                        for k in ("school_terms", "custom_days")
                    },
                }
                for key in ("school_terms", "custom_days"):
                    merged[key] = current[key]
            normalized = normalize_calendar(merged, strict=True)
            rev = current["revision"]
            self.store.save("calendar", "default", normalized, rev if rev > 0 else None)
            return self.get_calendar(scope)

    def save_trip(
        self,
        title: str,
        start_date: str,
        end_date: str,
        *,
        scope: str = "",
        wake_time: str = "",
        wake_window: int = 15,
        mode: str = "trip",
        note: str = "",
        trip_id: str = "",
    ) -> dict:
        """Create or update a multi-day trip / schedule override in calendar_config."""
        title = text_value(title, 80)
        start_d = text_value(start_date, 20)
        end_d = text_value(end_date, 20)
        if not title:
            raise AlbumError("请填写行程名称")
        try:
            if (
                not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_d)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_d)
                or date.fromisoformat(start_d) > date.fromisoformat(end_d)
            ):
                raise ValueError
        except ValueError:
            raise AlbumError(
                "请填写有效的起止日期（YYYY-MM-DD，且结束日期不早于开始日期）"
            ) from None
        wake_t = text_value(wake_time or "", 20)
        if wake_t and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", wake_t):
            raise AlbumError(
                "行程早起时间格式须为 HH:MM（如 08:00），留空表示不覆写早起时间"
            )
        with self.store.lock:
            cal = self.get_calendar()
            matched_id = trip_id
            if matched_id and not any(
                t["id"] == matched_id and (not scope or t.get("scope") == scope)
                for t in cal["trips"]
            ):
                raise AlbumError("未找到当前会话的对应行程")
            if not matched_id:
                for t in cal["trips"]:
                    if t.get("scope", "") == scope and (
                        t["title"] == title
                        or (t["start_date"] == start_d and t["end_date"] == end_d)
                    ):
                        matched_id = t["id"]
                        break
            trips = [t for t in cal["trips"] if t["id"] != matched_id]
            entry = {
                "id": matched_id or uid("trip_"),
                "scope": text_value(scope or "", 300),
                "title": title,
                "start_date": start_d,
                "end_date": end_d,
                "wake_time": wake_t,
                "wake_window": max(0, min(int(wake_window), 60)),
                "mode": mode if mode in TRIP_MODES else "trip",
                "note": text_value(note or "", 300),
            }
            trips.append(entry)
            trips.sort(key=lambda x: (x["start_date"], x["end_date"]))
            self.save_calendar({**cal, "trips": trips})
            return entry

    def delete_trip(self, trip_id: str, scope: str = "") -> dict:
        """Remove a multi-day trip override by ID, title, or latest active trip in scope if ID is empty."""
        with self.store.lock:
            cal = self.get_calendar()
            q = (trip_id or "").strip()
            if q == "all":
                removed = [
                    t for t in cal["trips"] if not scope or t.get("scope") == scope
                ]
                if not removed:
                    raise AlbumError("当前没有需要取消的特殊行程")
                kept = [t for t in cal["trips"] if t not in removed]
                self.save_calendar({**cal, "trips": kept})
                return removed[-1]
            target = None
            for t in cal["trips"]:
                in_scope = not scope or t.get("scope") == scope
                if (
                    q
                    and q != "latest"
                    and in_scope
                    and (t["id"] == q or q in t["title"])
                ):
                    target = t
                    break
                if (not q or q == "latest") and in_scope:
                    target = t
            if not target:
                raise AlbumError("未找到对应的特殊行程")
            trips = [t for t in cal["trips"] if t["id"] != target["id"]]
            self.save_calendar({**cal, "trips": trips})
            return target

    def update_calendar_from_chat(
        self,
        *,
        scope: str = "",
        winter_vacation: str = "",
        summer_vacation: str = "",
        exam_ranges: str = "",
        custom_day: str = "",
        remove_custom_day: str = "",
    ) -> dict:
        """Allow chat tool to update school vacation ranges, exam weeks, or custom festivals/anniversaries."""
        with self.store.lock:
            cal = self.get_calendar(scope)
            terms = dict(cal["school_terms"])
            custom_days = [dict(c) for c in cal["custom_days"]]
            changed = []
            if winter_vacation and "~" in winter_vacation:
                ws, we = [x.strip() for x in winter_vacation.split("~", 1)]
                terms["winter_start"], terms["winter_end"] = ws, we
                changed.append(f"寒假({ws}~{we})")
            if summer_vacation and "~" in summer_vacation:
                ss, se = [x.strip() for x in summer_vacation.split("~", 1)]
                terms["summer_start"], terms["summer_end"] = ss, se
                changed.append(f"暑假({ss}~{se})")
            if exam_ranges:
                terms["exam_ranges"] = exam_ranges.strip()
                changed.append(f"期末考试周({exam_ranges.strip()})")
            if remove_custom_day:
                rm = remove_custom_day.strip()
                before_len = len(custom_days)
                custom_days = [
                    c
                    for c in custom_days
                    if c["name"] != rm and c["date"] != normalize_custom_date_rule(rm)
                ]
                if len(custom_days) < before_len:
                    changed.append(f"移除节日/纪念日({rm})")
            if custom_day and ("|" in custom_day or "=" in custom_day):
                sep = "|" if "|" in custom_day else "="
                parts = [x.strip() for x in custom_day.split(sep)]
                rule = normalize_custom_date_rule(parts[0])
                name = parts[1][:60] if len(parts) > 1 else ""
                is_hol = (
                    parts[2] in ("放假", "true", "True", "1", "是")
                    if len(parts) > 2
                    else False
                )
                note = parts[3][:240] if len(parts) > 3 else ""
                if not rule or not name:
                    raise AlbumError(
                        "自定义节日格式须为：日期(MM-DD 或 农历MM-DD)|名称|放假或仅情绪|备注"
                    )
                custom_days = [
                    c
                    for c in custom_days
                    if not (c["name"] == name or c["date"] == rule)
                ]
                custom_days.append(
                    {"date": rule, "name": name, "is_holiday": is_hol, "note": note}
                )
                changed.append(f"节日/纪念日({rule} {name})")
            if not changed:
                raise AlbumError("未提供有效的时令日历修改项")
            saved = self.save_calendar(
                {"school_terms": terms, "custom_days": custom_days}, scope=scope
            )
            return {"changed": changed, "calendar": saved}

    def prune_expired_calendar(self, today: date) -> dict:
        """Auto-remove past-year holidays/workdays and trips that ended more than 3 days ago."""
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT data, revision FROM records WHERE kind='calendar' AND id='default'"
            ).fetchone()
            if not row:
                return self.get_calendar()
            raw = json.loads(row["data"])
            cal = normalize_calendar(raw, prune_before=today)
            trip_cutoff = (today - timedelta(days=3)).isoformat()
            active_trips = [
                t for t in cal["trips"] if t.get("end_date", "") >= trip_cutoff
            ]
            if (
                len(active_trips) != len(raw.get("trips", []))
                or len(cal["holidays"]) != len(raw.get("holidays", {}))
                or len(cal["workdays"]) != len(raw.get("workdays", {}))
            ):
                cal["trips"] = active_trips
                self.store.save("calendar", "default", cal, int(row["revision"]))
            return self.get_calendar()

    def override_wake_dates_for(self, scope: str = "") -> set[str]:
        """Return YYYY-MM-DD dates where a one-shot override_day_wake plan is pending or already ran."""
        dates = set()
        for p in self.store.all("life_plan"):
            if (
                not p.get("deleted")
                and p.get("enabled")
                and p.get("run_once")
                and p.get("override_day_wake")
                and (not scope or p.get("scope") == scope)
            ):
                if p.get("override_date"):
                    dates.add(p["override_date"])
                elif p.get("trigger_at"):
                    tz = ZoneInfo(p.get("timezone") or "Asia/Shanghai")
                    dates.add(
                        datetime.fromtimestamp(int(p["trigger_at"]), tz)
                        .date()
                        .isoformat()
                    )
        for r in self.store.all("life_run"):
            if (
                r.get("override_day_wake")
                and r.get("state") in ("running", "sent", "unknown")
                and (not scope or r.get("scope") == scope)
            ):
                if r.get("override_date"):
                    dates.add(r["override_date"])
        return dates

    def save(self, data: dict, *, validate_only: bool = False, character=None) -> dict:
        old = self.store.get("life_plan", data["id"]) if data.get("id") else {}
        if old.get("deleted"):
            raise AlbumError("请先从回收站恢复此计划")
        plan = {**old, **data}
        name = text_value(plan.get("name", ""), 120)
        instruction = text_value(plan.get("instruction", ""), 8000)
        if not name or not instruction:
            raise AlbumError("请填写计划名称和生活情境")
        scope = text_value(plan.get("scope", ""), 300)
        char = character or self.store.character_for(scope)
        timezone = text_value(plan.get("timezone", "Asia/Shanghai"), 100)
        run_once = plan.get("run_once", False)
        if type(run_once) is not bool:
            raise AlbumError("一次性标记格式不正确")
        trigger_at = plan.get("trigger_at", 0)
        cron = text_value(plan.get("cron", ""), 100)
        if run_once:
            try:
                ZoneInfo(timezone)
            except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
                raise AlbumError("请使用有效 IANA 时区") from exc
            if type(trigger_at) is not int or trigger_at <= 0:
                raise AlbumError("一次性约定需要有效的触发时间")
            trigger_at = (trigger_at // 60) * 60
            if not cron:
                dt = datetime.fromtimestamp(trigger_at, ZoneInfo(timezone))
                cron = f"{dt.minute} {dt.hour} {dt.day} {dt.month} *"
        else:
            triggers(cron, timezone)
            trigger_at = 0
        window_before = plan.get("window_before", 0)
        window_after = plan.get("window_after", 0)
        if (
            type(window_before) is not int
            or type(window_after) is not int
            or not (0 <= window_before <= MAX_WINDOW)
            or not (0 <= window_after <= MAX_WINDOW)
        ):
            raise AlbumError(f"浮动时间区间须为 0 至 {MAX_WINDOW} 分钟的整数")
        photo_mode = text_value(plan.get("photo_mode", "auto"), 20) or "auto"
        if photo_mode not in PHOTO_MODES:
            raise AlbumError("拍照模式须为 auto、always 或 never")
        day_mode = text_value(plan.get("day_mode", "cron"), 30) or "cron"
        if day_mode not in DAY_MODES:
            raise AlbumError("作息套组模式不正确")
        override_day_wake = bool(plan.get("override_day_wake", False))
        override_date = text_value(plan.get("override_date", ""), 20)
        if run_once and override_day_wake and not override_date and trigger_at > 0:
            override_date = (
                datetime.fromtimestamp(trigger_at, ZoneInfo(timezone))
                .date()
                .isoformat()
            )
        chain_depth = plan.get("chain_depth", 0)
        if type(chain_depth) is not int or not (0 <= chain_depth <= 2):
            raise AlbumError("计划链深度格式不正确")
        source = text_value(plan.get("source", "user"), 40) or "user"
        enabled = plan.get("enabled", False)
        if type(enabled) is not bool:
            raise AlbumError("启用状态格式不正确")
        payload = {
            "name": name,
            "instruction": instruction,
            "scope": scope,
            "character_id": char["id"],
            "cron": cron,
            "timezone": timezone,
            "enabled": enabled,
            "window_before": window_before,
            "window_after": window_after,
            "photo_mode": photo_mode,
            "day_mode": day_mode,
            "override_day_wake": override_day_wake,
            "override_date": override_date,
            "run_once": run_once,
            "trigger_at": trigger_at,
            "chain_depth": chain_depth,
            "source": source,
            "import_key": old.get("import_key", data.get("import_key", "")),
            "activated_at": now()
            if enabled and not old.get("enabled")
            else old.get("activated_at", 0),
        }
        if validate_only:
            return payload
        for other in self.store.all("life_plan"):
            if (
                other["id"] != old.get("id")
                and other["name"] == name
                and other["scope"] == scope
            ):
                raise AlbumError("此会话已存在同名计划，请编辑原计划")
        return self.store.save(
            "life_plan", old.get("id") or uid("plan_"), payload, data.get("revision")
        )

    def schedule_once(
        self,
        scope: str,
        name: str,
        trigger_at: int,
        instruction: str,
        *,
        window_minutes: int = 5,
        photo_mode: str = "auto",
        chain_depth: int = 0,
        source: str = "chat",
        override_day_wake: bool = False,
    ) -> dict:
        """Create and enable a one-shot plan that destroys itself after firing."""
        if chain_depth > 1:
            raise AlbumError("一次性后续约定最多延续一层，不再继续追加")
        current_ts = now()
        trigger_at = (int(trigger_at) // 60) * 60
        if trigger_at < current_ts + 180:
            raise AlbumError("约定时间至少需要在 3 分钟之后")
        if trigger_at > current_ts + 7 * 86400:
            raise AlbumError("一次性约定最多安排在 7 天以内")
        window_minutes = max(0, min(int(window_minutes), 30))
        window_before = min(window_minutes, (trigger_at - current_ts - 1) // 60)
        with self.store.lock:
            char = self.store.character_for(scope)
            existing = [
                p
                for p in self.store.all("life_plan")
                if p["scope"] == scope and not p.get("deleted")
            ]
            pending_once = [p for p in existing if p.get("run_once")]
            if len(pending_once) >= MAX_PENDING_ONCE:
                raise AlbumError(
                    f"当前已有 {MAX_PENDING_ONCE} 条待执行的心念约定，请等完成后再定新约定"
                )
            override_date = (
                datetime.fromtimestamp(trigger_at, ZoneInfo("Asia/Shanghai"))
                .date()
                .isoformat()
                if override_day_wake
                else ""
            )
            overrides = self.override_wake_dates_for(scope)
            if override_date:
                overrides.add(override_date)
            earliest = trigger_at - window_before * 60
            latest = trigger_at + window_minutes * 60
            cal = self.get_calendar()
            for other in existing:
                if not other.get("enabled"):
                    continue
                nearby = next_schedule_details(
                    other,
                    datetime.fromtimestamp(earliest - 1200 + 1, ZoneInfo("UTC")),
                    1,
                    calendar=cal,
                    override_wake_dates=overrides,
                )
                if nearby and nearby[0]["actual_ts"] < latest + 1200:
                    raise AlbumError(
                        "该时间窗口与当前会话其他计划相距不足 20 分钟，请换个时间"
                    )
            base_name = text_value(name, 100) or "心念约定"
            final_name = base_name
            existing_names = {p["name"] for p in existing}
            suffix = 2
            while final_name in existing_names:
                final_name = f"{base_name} ({suffix})"
                suffix += 1
            return self.save(
                {
                    "name": final_name,
                    "instruction": instruction,
                    "scope": scope,
                    "timezone": "Asia/Shanghai",
                    "enabled": True,
                    "run_once": True,
                    "trigger_at": trigger_at,
                    "window_before": window_before,
                    "window_after": window_minutes,
                    "photo_mode": photo_mode if photo_mode in PHOTO_MODES else "auto",
                    "chain_depth": chain_depth,
                    "source": source,
                    "override_day_wake": bool(override_day_wake),
                    "override_date": override_date,
                },
                character=char,
            )

    def cancel_once(self, scope: str, plan_id: str) -> dict:
        """Cancel and remove a pending one-shot plan in the given scope."""
        key = text_value(plan_id, 100)
        with self.store.lock:
            plan = self.store.get("life_plan", key)
            if plan["scope"] != scope or not plan.get("run_once"):
                raise AlbumError("只能取消当前会话中尚未触发的一次性心念约定")
            self.destroy_plan(key)
            return plan

    def destroy_plan(self, key: str):
        """Permanently delete a one-shot plan record from life_plan."""
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "DELETE FROM records WHERE kind='life_plan' AND id=?", (key,)
            )

    def save_recipient(self, data: dict, bots: list[dict]) -> dict:
        """Commit a visual QQ recipient binding and its plan in one transaction."""
        target = data.get("recipient")
        if not isinstance(target, dict):
            raise AlbumError("请选择机器人并填写接收对象")
        bot_id = text_value(target.get("bot_id", ""), 100)
        kind = target.get("message_type")
        number = text_value(target.get("number", ""), 12)
        if not any(b["id"] == bot_id for b in bots) or ":" in bot_id:
            raise AlbumError("此 QQ 机器人不可用，请刷新后重新选择")
        if kind not in ("FriendMessage", "GroupMessage") or not re.fullmatch(
            r"[1-9][0-9]{4,11}", number
        ):
            raise AlbumError("请选择私聊或群聊，并填写 5 至 12 位 QQ 号或群号")
        scope = f"{bot_id}:{kind}:{number}"
        with self.store.lock:
            char = self.store.get("character", target.get("character_id", ""))
            if (
                type(target.get("character_revision")) is not int
                or target["character_revision"] != char["revision"]
            ):
                raise AlbumError("角色绑定已更新，请刷新后重新编辑")
            for other in self.store.all("character"):
                if other["id"] != char["id"] and scope in other.get("sessions", []):
                    raise AlbumError("这个接收对象已绑定其他角色，请先核对原绑定")
            adding = scope not in char["sessions"]
            if adding and len(char["sessions"]) >= 100:
                raise AlbumError("角色绑定会话已达 100 个，请先整理绑定")
            payload = self.save(
                {**data, "scope": scope}, validate_only=True, character=char
            )
            if any(
                p["id"] != data.get("id")
                and p["scope"] == scope
                and p["name"] == payload["name"]
                for p in self.store.all("life_plan")
            ):
                raise AlbumError("此会话已存在同名计划，请编辑原计划")
            old = self.store.get("life_plan", data["id"]) if data.get("id") else {}
            if old and (
                type(data.get("revision")) is not int
                or data["revision"] != old["revision"]
            ):
                raise AlbumError("计划已更新，请刷新后重新编辑")
            key, revision = old.get("id") or uid("plan_"), old.get("revision", 0) + 1
            with self.store.db:
                if adding:
                    updated = {
                        k: v for k, v in char.items() if k not in ("id", "revision")
                    }
                    updated["sessions"] = [*char["sessions"], scope]
                    self.store.db.execute(
                        "UPDATE records SET data=?,revision=? WHERE kind='character' AND id=?",
                        (
                            json.dumps(updated, ensure_ascii=False),
                            char["revision"] + 1,
                            char["id"],
                        ),
                    )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO records VALUES ('life_plan',?,?,?)",
                    (key, json.dumps(payload, ensure_ascii=False), revision),
                )
            return self.store.get("life_plan", key)

    def import_data(self, payload: dict, scope: str = "") -> dict:
        if not isinstance(payload, dict) or payload.get("format") not in (
            FORMAT,
            "astrbot-cron-dashboard-extract-v1",
        ):
            raise AlbumError("不支持的计划文件格式")
        source = payload.get("plans", payload.get("jobs", []))
        if not isinstance(source, list) or not 1 <= len(source) <= 200:
            raise AlbumError("每次导入 1–200 条计划")
        prepared, keys = [], set()
        for item in source:
            if not isinstance(item, dict) or item.get("run_once"):
                raise AlbumError("当前导入支持周期生活计划，请先移除一次性任务")
            target = scope or item.get("scope", item.get("session", ""))
            identity = json.dumps([item.get("name"), target], ensure_ascii=False)
            key = hashlib.sha256(identity.encode()).hexdigest()
            if key in keys:
                raise AlbumError("文件含重复的计划名称和会话")
            keys.add(key)
            prepared.append(
                {
                    "name": item.get("name", ""),
                    "instruction": item.get("instruction", item.get("note", "")),
                    "cron": item.get("cron", item.get("cron_expression", "")),
                    "timezone": item.get("timezone") or "Asia/Shanghai",
                    "scope": target,
                    "enabled": False,
                    "window_before": int(item.get("window_before") or 0),
                    "window_after": int(item.get("window_after") or 0),
                    "photo_mode": item.get("photo_mode") or "auto",
                    "day_mode": item.get("day_mode") or "cron",
                    "import_key": key,
                }
            )
        existing = {(p["name"], p["scope"]) for p in self.store.all("life_plan")}
        created, skipped = [], 0
        # Validate the entire document before an atomic batch insert.
        with self.store.lock, self.store.db:
            for item in prepared:
                payload = self.save(item, validate_only=True)
                if (payload["name"], payload["scope"]) in existing:
                    skipped += 1
                else:
                    created.append(
                        (uid("plan_"), json.dumps(payload, ensure_ascii=False))
                    )
            self.store.db.executemany(
                "INSERT INTO records VALUES ('life_plan',?,?,1)", created
            )
        return {"created": len(created), "skipped": skipped, "enabled": False}

    def manage(self, patches: list[dict], action: str) -> dict:
        """Atomically enable, disable, trash or restore selected plans.

        Args:
            patches: Unique plan IDs and current revisions, at most 200.
            action: enable, disable, delete or restore; restoration stays disabled.
        """
        if action not in ("enable", "disable", "delete", "restore"):
            raise AlbumError("不支持的计划管理操作")
        if not isinstance(patches, list) or not 1 <= len(patches) <= 200:
            raise AlbumError("一次请选择1至200条计划")
        with self.store.lock:
            prepared, seen = [], set()
            for patch in patches:
                if not isinstance(patch, dict):
                    raise AlbumError("计划修改格式不正确")
                key = text_value(patch.get("id", ""), 100)
                old = self.store.get("life_plan", key)
                if (
                    key in seen
                    or type(patch.get("revision")) is not int
                    or patch["revision"] != old["revision"]
                ):
                    raise AlbumError("计划重复或已更新，请刷新后重新选择")
                seen.add(key)
                if old.get("deleted") and action not in ("delete", "restore"):
                    raise AlbumError("请先恢复回收站中的计划")
                payload = {k: v for k, v in old.items() if k not in ("id", "revision")}
                if action == "enable":
                    checked = self.save({**old, "enabled": True}, validate_only=True)
                    if checked["character_id"] != old["character_id"]:
                        raise AlbumError("会话已换绑角色，请先编辑并核对计划")
                    payload.update(checked)
                elif action == "disable":
                    payload["enabled"] = False
                elif action == "delete":
                    payload.update(enabled=False, deleted=True)
                elif old.get("deleted"):
                    payload.update(enabled=False)
                    payload.pop("deleted", None)
                if payload != {
                    k: v for k, v in old.items() if k not in ("id", "revision")
                }:
                    prepared.append(
                        (
                            json.dumps(payload, ensure_ascii=False),
                            old["revision"] + 1,
                            key,
                        )
                    )
            with self.store.db:
                self.store.db.executemany(
                    "UPDATE records SET data=?,revision=? WHERE kind='life_plan' AND id=?",
                    prepared,
                )
        return {"changed": len(prepared), "selected": len(patches), "action": action}

    def export_data(self) -> dict:
        return {
            "format": FORMAT,
            "exported_at": now(),
            "plans": [
                {
                    **{
                        k: p[k]
                        for k in (
                            "name",
                            "instruction",
                            "cron",
                            "timezone",
                            "scope",
                            "enabled",
                        )
                    },
                    **(
                        {"window_before": p["window_before"]}
                        if p.get("window_before")
                        else {}
                    ),
                    **(
                        {"window_after": p["window_after"]}
                        if p.get("window_after")
                        else {}
                    ),
                    **(
                        {"photo_mode": p["photo_mode"]}
                        if p.get("photo_mode") and p.get("photo_mode") != "auto"
                        else {}
                    ),
                    **(
                        {"day_mode": p["day_mode"]}
                        if p.get("day_mode") and p.get("day_mode") != "cron"
                        else {}
                    ),
                }
                for p in self.store.all("life_plan")
                if not p.get("deleted") and not p.get("run_once")
            ],
        }

    def state(self) -> dict:
        point = datetime.now(ZoneInfo("UTC"))
        sh_now = point.astimezone(ZoneInfo("Asia/Shanghai")).date()
        cal = self.get_calendar()
        plans = self.store.all("life_plan")
        runs = sorted(
            self.store.all("life_run"), key=lambda r: r.get("at", 0), reverse=True
        )
        last = {}
        zones = {p["id"]: p["timezone"] for p in plans}
        for run in runs:
            run.setdefault("timezone", zones.get(run.get("plan_id"), "Asia/Shanghai"))
            last.setdefault(run.get("plan_id"), run)
        override_cache = {}

        def _overrides(sc):
            if sc not in override_cache:
                override_cache[sc] = self.override_wake_dates_for(sc)
            return override_cache[sc]

        return {
            "plans": [
                {
                    **p,
                    "next_times": next_times(
                        p,
                        point,
                        calendar=cal,
                        override_wake_dates=_overrides(p.get("scope", "")),
                    )
                    if not p.get("deleted")
                    else [],
                    "next_details": next_schedule_details(
                        p,
                        point,
                        calendar=cal,
                        override_wake_dates=_overrides(p.get("scope", "")),
                    )
                    if not p.get("deleted")
                    else [],
                    "last_run": last.get(p["id"]),
                }
                for p in plans
            ],
            "runs": runs[:100],
            "calendar": cal,
            "today_profile": day_profile(sh_now, cal),
            "scheduler_error": self.last_error,
            "scheduler_running": self.task is not None and not self.task.done(),
        }

    async def start(self):
        for run in self.store.all("life_run"):
            if run["state"] == "running":
                self.store.save(
                    "life_run",
                    run["id"],
                    {
                        **run,
                        "state": "unknown",
                        "error": "上次执行中断，需核对聊天；不会自动重发",
                    },
                    run["revision"],
                )
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        # Deliberately skip downtime and the minute in which the plugin starts.
        previous = now() // 60
        while True:
            await asyncio.sleep(5)
            minute = now() // 60
            if minute != previous:
                previous = minute
                try:
                    await self.tick(minute * 60)
                    self.last_error = ""
                except Exception as exc:
                    self.last_error = "计划检查异常：" + type(exc).__name__

    def _matching_anchor_detail(
        self,
        plan: dict,
        timestamp: int,
        point: datetime,
        calendar: dict | None = None,
        override_wake_dates: set[str] | None = None,
    ) -> dict | None:
        """Return matching anchor info if the plan should fire at `timestamp`."""
        before = int(plan.get("window_before") or 0)
        after_win = int(plan.get("window_after") or 0)
        zone = ZoneInfo(plan["timezone"])
        if plan.get("run_once"):
            anchor_ts = (int(plan.get("trigger_at") or 0) // 60) * 60
            if anchor_ts <= 0:
                return None
            actual_ts = anchor_ts + jitter_offset(
                plan["id"], anchor_ts, before, after_win
            )
            if actual_ts == timestamp:
                local_d = datetime.fromtimestamp(actual_ts, zone).date()
                return {
                    "anchor_ts": anchor_ts,
                    "profile": day_profile(local_d, calendar, plan.get("scope", "")),
                    "trip_wake": False,
                }
            return None

        day_mode = plan.get("day_mode") or "cron"
        override_dates = override_wake_dates or set()
        has_trips = bool(calendar and calendar.get("trips"))

        if day_mode == "cron" and not override_dates and not has_trips:
            schedule = triggers(plan["cron"], plan["timezone"])
            if before <= 0 and after_win <= 0:
                if any(t.get_next_fire_time(None, point) == point for t in schedule):
                    local_d = point.astimezone(zone).date()
                    return {
                        "anchor_ts": timestamp,
                        "profile": day_profile(
                            local_d, calendar, plan.get("scope", "")
                        ),
                        "trip_wake": False,
                    }
                return None
            cursor = point - timedelta(minutes=after_win)
            end = point + timedelta(minutes=before)
            while cursor <= end:
                values = [t.get_next_fire_time(None, cursor) for t in schedule]
                value = min((v for v in values if v is not None), default=None)
                if value is None or value > end:
                    break
                anchor_ts = int(value.timestamp())
                actual_ts = anchor_ts + jitter_offset(
                    plan["id"], anchor_ts, before, after_win
                )
                if actual_ts == timestamp:
                    return {
                        "anchor_ts": anchor_ts,
                        "profile": day_profile(
                            value.date(), calendar, plan.get("scope", "")
                        ),
                        "trip_wake": False,
                    }
                cursor = value + timedelta(seconds=1)
            return None

        local_dt = point.astimezone(zone)
        for day_delta in (-1, 0, 1):
            cur_date = local_dt.date() + timedelta(days=day_delta)
            for item in _daily_anchors_for_plan(
                plan, cur_date, zone, calendar, override_dates
            ):
                anchor_ts = item["anchor_ts"]
                actual_ts = anchor_ts + jitter_offset(
                    plan["id"], anchor_ts, item["before"], item["after"]
                )
                if actual_ts == timestamp:
                    return item
        return None

    def _matching_anchor(
        self, plan: dict, timestamp: int, point: datetime
    ) -> int | None:
        detail = self._matching_anchor_detail(
            plan,
            timestamp,
            point,
            calendar=self.get_calendar(),
            override_wake_dates=self.override_wake_dates_for(plan.get("scope", "")),
        )
        return detail["anchor_ts"] if detail else None

    async def tick(self, timestamp: int):
        point = datetime.fromtimestamp(timestamp, ZoneInfo("UTC"))
        cal = (
            self.prune_expired_calendar(
                point.astimezone(ZoneInfo("Asia/Shanghai")).date()
            )
            if timestamp > 1700000000
            else self.get_calendar()
        )
        override_cache = {}
        for plan in self.store.all("life_plan"):
            if (
                plan.get("deleted")
                or not plan["enabled"]
                or plan.get("activated_at", 0) > timestamp
            ):
                continue
            # Clean up expired one-shot plans whose latest possible window has passed
            if plan.get("run_once"):
                max_fire = (
                    int(plan.get("trigger_at") or 0)
                    + int(plan.get("window_after") or 0) * 60
                )
                if max_fire > 0 and timestamp > max_fire + 3600:
                    self.destroy_plan(plan["id"])
                    continue
            sc = plan.get("scope", "")
            if sc not in override_cache:
                override_cache[sc] = self.override_wake_dates_for(sc)
            detail = self._matching_anchor_detail(
                plan,
                timestamp,
                point,
                calendar=cal,
                override_wake_dates=override_cache[sc],
            )
            if detail is None:
                continue
            anchor_ts = detail["anchor_ts"]
            key = f"{plan['id']}:{anchor_ts}"
            if self.store.db.execute(
                "SELECT 1 FROM records WHERE kind='life_run' AND id=?", (key,)
            ).fetchone():
                if plan.get("run_once"):
                    self.destroy_plan(plan["id"])
                continue
            zone = ZoneInfo(plan["timezone"])
            local = point.astimezone(zone)
            anchor_local = datetime.fromtimestamp(anchor_ts, zone)
            delta_minutes = (timestamp - anchor_ts) // 60
            busy = plan["scope"] in self.running_scopes or len(self.workers) >= 4
            run = self.store.save(
                "life_run",
                key,
                {
                    "plan_id": plan["id"],
                    "scope": plan["scope"],
                    "name": plan["name"],
                    "at": timestamp,
                    "anchor_at": anchor_ts,
                    "delta_minutes": delta_minutes,
                    "run_once": bool(plan.get("run_once")),
                    "override_day_wake": bool(plan.get("override_day_wake")),
                    "override_date": plan.get("override_date", ""),
                    "trip_wake": bool(detail.get("trip_wake")),
                    "day_label": detail["profile"]["label"],
                    "timezone": plan["timezone"],
                    "state": "skipped" if busy else "running",
                    "error": "上一生活片刻仍在处理中，本次跳过" if busy else "",
                },
            )
            if run["state"] == "skipped":
                if plan.get("run_once"):
                    self.destroy_plan(plan["id"])
                continue
            if plan.get("override_day_wake") and plan.get("override_date"):
                override_cache[sc].add(plan["override_date"])
            self.running_scopes.add(plan["scope"])
            effective_instruction = plan["instruction"]
            if detail.get("trip_wake") and detail["profile"].get("active_trip"):
                trip = detail["profile"]["active_trip"]
                effective_instruction = (
                    f"【特殊行程早起覆写】当前处于特殊行程「{trip['title']}」（第 {trip['day_index']}/{trip['total_days']} 天，"
                    f"{trip['start_date']} ~ {trip['end_date']}），今天不再睡到中午懒觉，而是按行程约定在 {trip['wake_time']} 左右早起。"
                    + (f"行程备注：{trip['note']}。" if trip.get("note") else "")
                    + "请自然向对方打招呼，分享早起准备出发/洗漱/期待今天行程的心情（或带一点刚睡醒的小迷糊），可顺便拍一张清晨准备出发的照片。\n\n原常驻作息参考："
                    + plan["instruction"]
                )
            plan_with_meta = {
                **plan,
                "_anchor_ts": anchor_ts,
                "_anchor_local": anchor_local.isoformat(),
                "_delta_minutes": delta_minutes,
                "_day_profile": detail["profile"],
                "_trip_wake": bool(detail.get("trip_wake")),
            }
            task = asyncio.create_task(
                self.run(
                    plan_with_meta,
                    run,
                    effective_instruction,
                    local.isoformat(),
                )
            )
            self.workers.add(task)
            task.add_done_callback(self.workers.discard)

    async def run(self, plan: dict, run: dict, instruction: str, local_time: str):
        destroy = False
        try:
            latest = self.store.get("life_plan", plan["id"])
            if (
                latest.get("deleted")
                or not latest["enabled"]
                or latest["revision"] != plan["revision"]
            ):
                self.store.save(
                    "life_run",
                    run["id"],
                    {**run, "state": "held", "error": "计划已停用、删除或修改"},
                    run["revision"],
                )
                return
            destroy = bool(latest.get("run_once"))
            status, receipt = await self.execute(
                plan, run["id"], instruction, local_time
            )
            self.store.save(
                "life_run",
                run["id"],
                {**run, "state": status, **receipt},
                run["revision"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.save(
                "life_run",
                run["id"],
                {
                    **run,
                    "state": "unknown",
                    "error": "执行异常，需核对；不会自动重发",
                    "code": type(exc).__name__,
                },
                run["revision"],
            )
        finally:
            if destroy:
                try:
                    self.destroy_plan(plan["id"])
                except Exception:
                    pass
            self.running_scopes.discard(plan["scope"])

    async def close(self):
        tasks = list(self.workers) + ([self.task] if self.task else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
