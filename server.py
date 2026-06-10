#!/usr/bin/env python3
"""Local Navi100-compatible backend for the open-access replica.

It serves the captured frontend and a compatible API surface so the UI can be
exercised end to end without an activation gate.
"""

from __future__ import annotations

import concurrent.futures
import json
import mimetypes
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
FUNDS_FILE = ROOT / "work" / "funds.json"
NAVI_UPSTREAM_BASE = os.environ.get("NAVI_UPSTREAM_BASE", "https://www.navi100.top").rstrip("/")
FUNDS_CACHE_TTL = int(os.environ.get("FUNDS_CACHE_TTL_SECONDS", "900"))
MARKET_CACHE_TTL = int(os.environ.get("MARKET_CACHE_TTL_SECONDS", "900"))
HTTP_TIMEOUT = float(os.environ.get("NAVI_HTTP_TIMEOUT_SECONDS", "5"))
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "close",
    "Referer": "https://quote.eastmoney.com/",
}
CN_TZ = timezone(timedelta(hours=8))
SSL_CONTEXT = ssl._create_unverified_context()
FUNDS_CACHE: dict[str, object] = {"expires_at": 0.0, "payload": None}
MARKET_CACHE: dict[str, object] = {"expires_at": 0.0, "payload": None}
FUNDS_REFRESH_LOCK = threading.Lock()
FUNDS_REFRESH_IN_PROGRESS = False
SINA_HEADERS = {
    **HTTP_HEADERS,
    "Accept": "*/*",
    "Referer": "https://finance.sina.com.cn/",
}

MARKET_SNAPSHOT = {
    "market_date": "2026-06-05",
    "fetch_time": "2026-06-08 09:19:00",
    "ndx_close": 21815.67,
    "ndx_prev_close": 21975.91,
    "spx_close": 6000.36,
    "spx_prev_close": 5939.30,
    "vix_close": 16.77,
    "us10y": 4.51,
}

PE_SNAPSHOT = {
    "nasdaq_pe": 31.49,
    "nasdaq_pe_date": "09 June 2026",
    "nasdaq_pe_percentile": 0.8329519450800915,
    "nasdaq_pe_fair_low": 27.31,
    "nasdaq_pe_fair_high": 33.23,
    "nasdaq_pe_label": "合理",
    "nasdaq_pe_source": "World PE Ratio cached snapshot",
    "nasdaq_pe_url": "https://worldperatio.com/index/nasdaq-100/",
}

EASTMONEY_QUOTE_FIELDS = "f43,f44,f45,f46,f47,f57,f58,f60,f86,f169,f170"
EASTMONEY_FUND_QUOTE_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f86,f169,f170,f292"
EASTMONEY_KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
EASTMONEY_KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58"
WORLD_PE_URL = "https://worldperatio.com/index/nasdaq-100/"
MARKET_SYMBOLS = {
    "ndx": {"secid": "100.NDX", "name": "纳指100"},
    "spx": {"secid": "100.SPX", "name": "标普500"},
    "us10y": {"secid": "171.US10Y", "name": "美国10年期国债收益率"},
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def observed_fetch_time() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S")


def json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_bytes(
    url: str,
    *,
    timeout: float = HTTP_TIMEOUT,
    headers: dict[str, str] | None = None,
    attempts: int = 2,
) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=headers or HTTP_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
                return resp.read()
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(0.35 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("request failed")


def request_text(
    url: str,
    *,
    timeout: float = HTTP_TIMEOUT,
    encoding: str = "utf-8",
    headers: dict[str, str] | None = None,
    attempts: int = 2,
) -> str:
    return request_bytes(url, timeout=timeout, headers=headers, attempts=attempts).decode(encoding, "replace")


def request_json(url: str, *, timeout: float = HTTP_TIMEOUT) -> dict[str, object]:
    payload = json.loads(request_text(url, timeout=timeout))
    return payload if isinstance(payload, dict) else {}


def cache_get(cache: dict[str, object]) -> dict[str, object] | None:
    payload = cache.get("payload")
    if isinstance(payload, dict) and float(cache.get("expires_at") or 0) > time.time():
        return payload
    return None


def cache_set(cache: dict[str, object], payload: dict[str, object], ttl: int) -> dict[str, object]:
    cache["payload"] = payload
    cache["expires_at"] = time.time() + ttl
    return payload


def cache_payload(cache: dict[str, object]) -> dict[str, object] | None:
    payload = cache.get("payload")
    return dict(payload) if isinstance(payload, dict) else None


def load_funds_payload() -> dict[str, object]:
    with FUNDS_FILE.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["success"] = True
    return payload


def full_date(text: object) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d{2}-\d{2}", raw):
        return f"{datetime.now(CN_TZ).year}-{raw}"
    return raw[:10]


def timestamp_text(timestamp: object) -> str:
    value = int(as_float(timestamp, 0))
    if not value:
        return ""
    return datetime.fromtimestamp(value, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def exchange_secid(code: str) -> str:
    return ("1." if code.startswith("5") else "0.") + code


def eastmoney_fund_estimate(code: str) -> dict[str, object]:
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time() * 1000)}"
    text = request_text(url, timeout=3)
    match = re.search(r"jsonpgz\((\{.*\})\);?", text)
    if not match:
        raise ValueError(f"missing fund estimate for {code}")
    data = json.loads(match.group(1))
    return data if isinstance(data, dict) else {}


def eastmoney_pingzhong_latest(code: str) -> dict[str, object]:
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js?v={int(time.time() * 1000)}"
    text = request_text(url, timeout=5)
    trend_match = re.search(r"var Data_netWorthTrend = (\[.*?\]);", text, re.S)
    if not trend_match:
        raise ValueError(f"missing net worth trend for {code}")
    trend = json.loads(trend_match.group(1))
    if not isinstance(trend, list) or not trend:
        raise ValueError(f"empty net worth trend for {code}")
    latest = trend[-1]
    if not isinstance(latest, dict):
        raise ValueError(f"invalid net worth trend for {code}")
    timestamp = int(as_float(latest.get("x"), 0)) / 1000
    one_year = re.search(r'var syl_1n="([^"]*)"', text)
    return {
        "latest_nav": as_float(latest.get("y"), float("nan")),
        "daily_change_pct": as_float(latest.get("equityReturn"), float("nan")),
        "nav_date": datetime.fromtimestamp(timestamp, CN_TZ).strftime("%Y-%m-%d") if timestamp else "",
        "one_year_return_pct": as_float(one_year.group(1), float("nan")) if one_year else float("nan"),
    }


def sina_fund_snapshot(code: str) -> dict[str, object]:
    text = request_text(
        f"https://hq.sinajs.cn/list=f_{code}",
        timeout=3,
        encoding="gb18030",
        headers=SINA_HEADERS,
    )
    match = re.search(r'="([^"]*)"', text)
    if not match:
        raise ValueError(f"missing sina fund snapshot for {code}")
    parts = match.group(1).split(",")
    if len(parts) < 5:
        raise ValueError(f"incomplete sina fund snapshot for {code}")
    return {
        "latest_nav": as_float(parts[1], float("nan")),
        "accumulated_nav": as_float(parts[2], float("nan")),
        "iopv": as_float(parts[3], float("nan")),
        "nav_date": full_date(parts[4]),
        "fund_scale_billion": as_float(parts[5], float("nan")) if len(parts) > 5 else float("nan"),
    }


def sina_cn_exchange_quote(code: str) -> dict[str, object]:
    symbol = ("sh" if code.startswith("5") else "sz") + code
    text = request_text(
        f"https://hq.sinajs.cn/list={symbol}",
        timeout=3,
        encoding="gb18030",
        headers=SINA_HEADERS,
    )
    match = re.search(r'="([^"]*)"', text)
    if not match:
        raise ValueError(f"missing Sina CN quote for {code}")
    parts = match.group(1).split(",")
    if len(parts) < 32 or not parts[3]:
        raise ValueError(f"incomplete Sina CN quote for {code}")
    prev = as_float(parts[2], float("nan"))
    price = as_float(parts[3], float("nan"))
    if price != price or prev != prev:
        raise ValueError(f"invalid Sina CN quote for {code}")
    quote_time = f"{parts[30]} {parts[31]}".strip()
    return {
        "latest_price": price,
        "price_prev_close": prev,
        "daily_change_pct": (price - prev) / prev * 100 if prev else 0.0,
        "daily_change_date": quote_time,
        "price_date": quote_time,
        "turnover_amount": as_float(parts[9], float("nan")),
    }


def eastmoney_exchange_fund_quote(code: str) -> dict[str, object]:
    secid = exchange_secid(code)
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={EASTMONEY_FUND_QUOTE_FIELDS}"
    payload = request_json(url, timeout=3)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"missing exchange fund quote for {code}")
    price = parse_eastmoney_scaled(data.get("f43"), scale=1000.0)
    prev = parse_eastmoney_scaled(data.get("f60"), scale=1000.0)
    if price is None or prev is None:
        raise ValueError(f"incomplete exchange fund quote for {code}")
    quote_time = timestamp_text(data.get("f86"))
    change_pct = parse_eastmoney_scaled(data.get("f170"), scale=100.0)
    return {
        "latest_price": price,
        "price_prev_close": prev,
        "daily_change_pct": change_pct if change_pct is not None else ((price - prev) / prev * 100 if prev else 0.0),
        "daily_change_date": quote_time or "",
        "price_date": quote_time or "",
        "turnover_amount": as_float(data.get("f48"), float("nan")),
    }


def update_fund_record(record: dict[str, object]) -> dict[str, object]:
    item = dict(record)
    code = str(item.get("code") or "").strip()
    errors: list[str] = []
    if not code:
        return item

    try:
        estimate = eastmoney_fund_estimate(code)
        if estimate.get("dwjz") not in (None, ""):
            item["latest_nav"] = as_float(estimate.get("dwjz"), item.get("latest_nav"))
        if estimate.get("jzrq"):
            item["nav_date"] = full_date(estimate.get("jzrq"))
        if estimate.get("gsz") not in (None, ""):
            item["estimate_nav"] = as_float(estimate.get("gsz"))
        if estimate.get("gszzl") not in (None, ""):
            item["estimate_change_pct"] = as_float(estimate.get("gszzl"))
        if estimate.get("gztime"):
            item["estimate_time"] = str(estimate.get("gztime"))
        if item.get("trade_mode") != "exchange" and estimate.get("gszzl") not in (None, ""):
            item["daily_change_pct"] = as_float(estimate.get("gszzl"), item.get("daily_change_pct"))
            item["daily_change_date"] = str(estimate.get("gztime") or item.get("nav_date") or "")[:16]
            item["daily_change_source"] = "Eastmoney fund estimate"
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        try:
            latest = eastmoney_pingzhong_latest(code)
            for key in ("latest_nav", "nav_date", "one_year_return_pct"):
                value = latest.get(key)
                if value not in (None, "") and value == value:
                    item[key] = value
            if latest.get("daily_change_pct") == latest.get("daily_change_pct"):
                item["daily_change_pct"] = latest.get("daily_change_pct")
                item["daily_change_date"] = str(latest.get("nav_date") or "")
                item["daily_change_source"] = "Eastmoney disclosed NAV"
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as latest_exc:
            errors.append(f"fund_estimate: {exc}")
            errors.append(f"fund_latest_nav: {latest_exc}")

    if item.get("trade_mode") == "exchange":
        try:
            item.update(sina_cn_exchange_quote(code))
            item["daily_change_source"] = "Sina CN exchange quote"
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            errors.append(f"sina_exchange_quote: {exc}")
            try:
                item.update(eastmoney_exchange_fund_quote(code))
                item["daily_change_source"] = "Eastmoney exchange quote"
            except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as em_exc:
                errors.append(f"exchange_quote: {em_exc}")

        try:
            snapshot = sina_fund_snapshot(code)
            for key in ("latest_nav", "accumulated_nav", "iopv", "nav_date"):
                value = snapshot.get(key)
                if value not in (None, "") and value == value:
                    item[key] = value
            scale = snapshot.get("fund_scale_billion")
            if isinstance(scale, (int, float)) and scale == scale and scale > 0:
                item["fund_scale_billion"] = scale
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            errors.append(f"sina_fund: {exc}")

        price = as_float(item.get("latest_price"), float("nan"))
        iopv = as_float(item.get("iopv"), float("nan"))
        nav = as_float(item.get("latest_nav"), float("nan"))
        premium_base = iopv if iopv == iopv and iopv > 0 else nav
        if price == price and premium_base == premium_base and premium_base > 0:
            item["premium_pct"] = (price - premium_base) / premium_base * 100
            item["premium_source"] = "iopv" if iopv == iopv and iopv > 0 else "nav"
            item["premium_date"] = str(item.get("price_date") or item.get("nav_date") or "")[:16]

    item["data_updated_at"] = observed_fetch_time()
    item["source"] = "Eastmoney fund estimate + exchange quote + Sina fund snapshot"
    if errors:
        item["live_errors"] = errors
    return item


def refresh_funds_payload(base_payload: dict[str, object]) -> dict[str, object]:
    records = [item for item in base_payload.get("records", []) if isinstance(item, dict)]
    errors: list[str] = []
    updated: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for item in executor.map(update_fund_record, records):
            updated.append(item)
            if item.get("live_errors"):
                errors.extend([f"{item.get('code')}: {err}" for err in item.get("live_errors", [])])
    payload = dict(base_payload)
    payload["records"] = updated
    payload["base_count"] = len(records)
    payload["count"] = len(updated)
    payload["dynamic_fetched_at"] = observed_fetch_time()
    payload["source_mode"] = "live_direct_partial" if errors else "live_direct"
    payload["data_sources"] = {
        "fund_estimate": "Eastmoney fundgz",
        "exchange_quote": "Sina CN exchange quote / Eastmoney push2",
        "exchange_iopv": "Sina fund quote",
    }
    payload["errors"] = errors[:30]
    payload["success"] = True
    return payload


def refresh_funds_cache_background() -> None:
    global FUNDS_REFRESH_IN_PROGRESS
    try:
        payload = refresh_funds_payload(load_funds_payload())
        cache_set(FUNDS_CACHE, payload, FUNDS_CACHE_TTL)
    except Exception as exc:
        stale = cache_payload(FUNDS_CACHE) or load_funds_payload()
        stale["source_mode"] = stale.get("source_mode") or "local_snapshot"
        stale["errors"] = list(stale.get("errors") or []) + [f"background_funds_refresh: {exc}"]
        cache_set(FUNDS_CACHE, stale, min(FUNDS_CACHE_TTL, 300))
    finally:
        with FUNDS_REFRESH_LOCK:
            FUNDS_REFRESH_IN_PROGRESS = False


def schedule_funds_refresh() -> bool:
    global FUNDS_REFRESH_IN_PROGRESS
    with FUNDS_REFRESH_LOCK:
        if FUNDS_REFRESH_IN_PROGRESS:
            return False
        FUNDS_REFRESH_IN_PROGRESS = True
    thread = threading.Thread(target=refresh_funds_cache_background, name="funds-refresh", daemon=True)
    thread.start()
    return True


def load_live_funds_payload(*, force_refresh: bool = False, prefer_fast: bool = True) -> dict[str, object]:
    cached = None if force_refresh else cache_get(FUNDS_CACHE)
    if cached is not None:
        payload = dict(cached)
        payload["cache"] = "memory"
        payload["success"] = True
        return payload

    if prefer_fast and not force_refresh:
        stale_payload = cache_payload(FUNDS_CACHE)
        payload = stale_payload or load_funds_payload()
        payload["success"] = True
        payload["cache"] = "memory_stale" if stale_payload else "local_snapshot"
        payload["source_mode"] = payload.get("source_mode") or "local_snapshot"
        payload["refreshing"] = schedule_funds_refresh()
        return payload

    payload = load_funds_payload()
    try:
        return cache_set(FUNDS_CACHE, refresh_funds_payload(payload), FUNDS_CACHE_TTL)
    except Exception as exc:
        payload["source_mode"] = "local_snapshot"
        payload["errors"] = list(payload.get("errors") or []) + [f"direct_funds_refresh: {exc}"]
        return payload


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_eastmoney_scaled(value: object, scale: float = 100.0) -> float | None:
    number = as_float(value, float("nan"))
    if number != number or number == -1:
        return None
    return number / scale


def eastmoney_quote(secid: str, *, scale: float = 100.0) -> dict[str, object]:
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={EASTMONEY_QUOTE_FIELDS}"
    payload = request_json(url, timeout=4)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"missing quote data for {secid}")
    close = parse_eastmoney_scaled(data.get("f43"), scale=scale)
    prev_close = parse_eastmoney_scaled(data.get("f60"), scale=scale)
    if close is None or prev_close is None:
        raise ValueError(f"incomplete quote data for {secid}")
    timestamp = int(as_float(data.get("f86"), 0))
    quote_time = timestamp_text(timestamp)
    quote_date = quote_time[:10] if quote_time else ""
    return {
        "close": close,
        "prev_close": prev_close,
        "change_pct": (close - prev_close) / prev_close * 100 if prev_close else 0.0,
        "date": quote_date,
        "time": quote_time,
        "name": data.get("f58") or secid,
    }


def eastmoney_klines(secid: str, *, months: int = 18) -> list[dict[str, float | str]]:
    begin = (now_utc() - timedelta(days=months * 31)).strftime("%Y%m%d")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1={EASTMONEY_KLINE_FIELDS1}&fields2={EASTMONEY_KLINE_FIELDS2}"
        f"&klt=101&fqt=0&beg={begin}&end=20500101"
    )
    payload = request_json(url, timeout=4)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("klines"), list):
        raise ValueError(f"missing kline data for {secid}")
    rows: list[dict[str, float | str]] = []
    for row in data["klines"]:
        parts = str(row).split(",")
        if len(parts) < 3:
            continue
        close = as_float(parts[2], float("nan"))
        if close != close:
            continue
        rows.append({"date": parts[0], "open": as_float(parts[1]), "close": close})
    if len(rows) < 2:
        raise ValueError(f"not enough kline data for {secid}")
    return rows


def yahoo_chart(symbol: str, *, range_text: str = "18mo", interval: str = "1d") -> dict[str, object]:
    encoded = quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={range_text}&interval={interval}"
    payload = request_json(url, timeout=6)
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise ValueError(f"missing Yahoo chart for {symbol}")
    result = chart.get("result")
    if not isinstance(result, list) or not result:
        raise ValueError(f"empty Yahoo chart for {symbol}")
    first = result[0]
    if not isinstance(first, dict):
        raise ValueError(f"invalid Yahoo chart for {symbol}")
    return first


def yahoo_quote(symbol: str, name: str) -> dict[str, object]:
    data = yahoo_chart(symbol, range_text="5d")
    meta = data.get("meta")
    timestamps = data.get("timestamp")
    if not isinstance(meta, dict) or not isinstance(timestamps, list) or not timestamps:
        raise ValueError(f"incomplete Yahoo quote for {symbol}")
    close = as_float(meta.get("regularMarketPrice"), float("nan"))
    prev_close = as_float(meta.get("chartPreviousClose") or meta.get("previousClose"), float("nan"))
    if close != close or prev_close != prev_close:
        raise ValueError(f"missing Yahoo price for {symbol}")
    quote_date = datetime.fromtimestamp(int(timestamps[-1]), timezone.utc).strftime("%Y-%m-%d")
    return {
        "close": close,
        "prev_close": prev_close,
        "change_pct": (close - prev_close) / prev_close * 100 if prev_close else 0.0,
        "date": quote_date,
        "time": quote_date,
        "name": name,
    }


def yahoo_klines(symbol: str) -> list[dict[str, float | str]]:
    data = yahoo_chart(symbol)
    timestamps = data.get("timestamp")
    indicators = data.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, dict):
        raise ValueError(f"incomplete Yahoo history for {symbol}")
    quote_rows = indicators.get("quote")
    if not isinstance(quote_rows, list) or not quote_rows or not isinstance(quote_rows[0], dict):
        raise ValueError(f"missing Yahoo quote rows for {symbol}")
    closes = quote_rows[0].get("close")
    opens = quote_rows[0].get("open")
    if not isinstance(closes, list):
        raise ValueError(f"missing Yahoo closes for {symbol}")
    rows: list[dict[str, float | str]] = []
    for idx, timestamp in enumerate(timestamps):
        close = as_float(closes[idx] if idx < len(closes) else None, float("nan"))
        if close != close:
            continue
        open_value = as_float(opens[idx] if isinstance(opens, list) and idx < len(opens) else close)
        rows.append(
            {
                "date": datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%d"),
                "open": open_value,
                "close": close,
            }
        )
    if len(rows) < 2:
        raise ValueError(f"not enough Yahoo history for {symbol}")
    return rows


def sina_us_index_quote(symbol: str, name: str) -> dict[str, object]:
    text = request_text(
        f"https://hq.sinajs.cn/list={symbol}",
        timeout=4,
        encoding="gb18030",
        headers=SINA_HEADERS,
    )
    match = re.search(r'="([^"]*)"', text)
    if not match:
        raise ValueError(f"missing Sina index quote for {symbol}")
    parts = match.group(1).split(",")
    if len(parts) < 5 or not parts[1]:
        raise ValueError(f"incomplete Sina index quote for {symbol}")
    close = as_float(parts[1], float("nan"))
    change_pct = as_float(parts[2], float("nan"))
    change_value = as_float(parts[4], float("nan"))
    prev_close = close - change_value if change_value == change_value else close / (1 + change_pct / 100)
    quote_time = parts[3] if len(parts) > 3 else ""
    if close != close or prev_close != prev_close:
        raise ValueError(f"invalid Sina index quote for {symbol}")
    return {
        "close": close,
        "prev_close": prev_close,
        "change_pct": change_pct if change_pct == change_pct else ((close - prev_close) / prev_close * 100 if prev_close else 0.0),
        "date": full_date(quote_time),
        "time": quote_time,
        "name": name,
    }


def sina_us_klines(symbol: str) -> list[dict[str, float | str]]:
    url = (
        "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_DailyK=/"
        f"US_MinKService.getDailyK?symbol={quote(symbol, safe='')}&___qn=3"
    )
    text = request_text(url, timeout=8, headers=SINA_HEADERS)
    match = re.search(r"var _DailyK=\((.*)\);?", text, re.S)
    if not match:
        raise ValueError(f"missing Sina daily rows for {symbol}")
    data = json.loads(match.group(1))
    if not isinstance(data, list):
        raise ValueError(f"invalid Sina daily rows for {symbol}")
    rows: list[dict[str, float | str]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        close = as_float(row.get("c"), float("nan"))
        if close != close:
            continue
        rows.append({"date": str(row.get("d") or ""), "open": as_float(row.get("o")), "close": close})
    if len(rows) < 2:
        raise ValueError(f"not enough Sina daily rows for {symbol}")
    return rows


def cnbc_us10y_quote() -> dict[str, object]:
    text = request_text("https://quote.cnbc.com/quote-html-webservice/quote.htm?symbols=US10Y", timeout=5)

    def tag(name: str) -> str:
        match = re.search(rf"<{name}>([^<]*)", text)
        return match.group(1).strip() if match else ""

    last = as_float(tag("last"), float("nan"))
    if last != last:
        raise ValueError("missing CNBC US10Y quote")
    cached = tag("cachedTime")
    year_match = re.search(r"(20\d{2})", cached)
    month_match = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b\s+(\d{1,2})", cached)
    date_text = ""
    if year_match and month_match:
        month = datetime.strptime(month_match.group(1), "%b").month
        date_text = f"{year_match.group(1)}-{month:02d}-{int(month_match.group(2)):02d}"
    return {"close": last, "date": date_text or cached, "name": tag("name") or "U.S. 10 Year Treasury"}


def cboe_vix_history() -> list[dict[str, float | str]]:
    text = request_text("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", timeout=8)
    rows: list[dict[str, float | str]] = []
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        close = as_float(parts[4], float("nan"))
        if close != close:
            continue
        date_text = parts[0]
        try:
            date_text = datetime.strptime(date_text, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
        rows.append({"date": date_text, "close": close})
    if len(rows) < 2:
        raise ValueError("not enough VIX history")
    return rows


def percentile_rank(value: float, history: list[float]) -> float:
    if not history:
        return 0.5
    below = len([item for item in history if item <= value])
    return below / len(history)


def trailing_return(rows: list[dict[str, float | str]], days: int) -> float:
    if len(rows) <= days:
        return 0.0
    latest = as_float(rows[-1].get("close"))
    previous = as_float(rows[-1 - days].get("close"))
    return (latest - previous) / previous * 100 if previous else 0.0


def build_nasdaq_pe_snapshot() -> dict[str, object]:
    html = request_text(WORLD_PE_URL, timeout=5, attempts=1)
    current = re.search(
        r"P/E\) Ratio</b> for <b>Nasdaq 100 Index</b> is <b>([0-9.]+)</b>, calculated on <b>([^<]+)</b>",
        html,
    )
    if not current:
        current = re.search(r"Current P/E Ratio.*?<div class=\"badge-value\">([0-9.]+)</div>", html, re.S)
    if not current:
        raise ValueError("missing Nasdaq 100 PE value")

    pe_value = as_float(current.group(1), float("nan"))
    if pe_value != pe_value or pe_value <= 0:
        raise ValueError("invalid Nasdaq 100 PE value")

    pe_date = current.group(2).strip() if current.lastindex and current.lastindex >= 2 else ""
    interval_match = re.search(r"average P/E interval is \[([0-9.]+)\s*,\s*([0-9.]+)\]", html)
    fair_low = as_float(interval_match.group(1), 0.0) if interval_match else 0.0
    fair_high = as_float(interval_match.group(2), 0.0) if interval_match else 0.0
    history = [
        as_float(value, float("nan"))
        for value in re.findall(r'\{"x":([0-9.]+),"y":[^,]+,"name":"[^"]+","range":"5Y"\}', html)
    ]
    history = [value for value in history if value == value and value > 0]
    pe_percentile = percentile_rank(pe_value, history) if history else 0.5
    if fair_low and pe_value < fair_low:
        label = "偏低"
    elif fair_high and pe_value > fair_high:
        label = "偏高"
    else:
        label = "合理"
    return {
        "nasdaq_pe": pe_value,
        "nasdaq_pe_date": pe_date,
        "nasdaq_pe_percentile": pe_percentile,
        "nasdaq_pe_fair_low": fair_low or None,
        "nasdaq_pe_fair_high": fair_high or None,
        "nasdaq_pe_label": label,
        "nasdaq_pe_source": "World PE Ratio, QQQ-based estimate",
        "nasdaq_pe_url": WORLD_PE_URL,
    }


def score_plain(name: str, score: float, values: dict[str, float]) -> str:
    if name == "估值位置":
        pct = values.get("valuation_percentile", 0.5) * 100
        pe = values.get("nasdaq_pe", 0)
        if pe > 0:
            return f"纳指100 PE {pe:.2f}，处在近5年约 {pct:.0f}% 分位；PE越高，估值温度越热。"
        if score >= 7:
            return f"主要指数接近近一年高位（约 {pct:.0f}% 分位），买入节奏宜放慢。"
        if score <= 3:
            return f"主要指数处在近一年低位（约 {pct:.0f}% 分位），可提高分批买入力度。"
        return f"主要指数处在近一年中部区域（约 {pct:.0f}% 分位），估值温度中性。"
    if name == "波动水平":
        vix = values.get("vix", 0)
        if score <= 3:
            return f"VIX {vix:.2f}，市场恐慌升温，按纪律分批承接。"
        if score >= 7:
            return f"VIX {vix:.2f}，波动偏低，市场情绪较平稳甚至偏热。"
        return f"VIX {vix:.2f}，波动处在常态区间。"
    if name == "趋势强弱":
        ret20 = values.get("ret20", 0)
        ret60 = values.get("ret60", 0)
        return f"近 20 日约 {ret20:+.1f}%，近 60 日约 {ret60:+.1f}%，用于判断短中期趋势。"
    y = values.get("us10y", 0)
    if score >= 7:
        return f"10 年期美债收益率 {y:.3f}%，利率压力偏高。"
    if score <= 3:
        return f"10 年期美债收益率 {y:.3f}%，利率环境相对友好。"
    return f"10 年期美债收益率 {y:.3f}%，利率环境中性。"


def build_market_snapshot(*, force_refresh: bool = False) -> dict[str, object]:
    cached = None if force_refresh else cache_get(MARKET_CACHE)
    if cached is not None:
        return dict(cached)

    errors: list[str] = []
    ndx_rows: list[dict[str, float | str]] = []
    spx_rows: list[dict[str, float | str]] = []
    vix_rows: list[dict[str, float | str]] = []

    try:
        ndx_quote = sina_us_index_quote("gb_ndx", "纳指100")
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        errors.append(f"ndx_sina_quote: {exc}")
        try:
            ndx_quote = eastmoney_quote(MARKET_SYMBOLS["ndx"]["secid"])
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as em_exc:
            errors.append(f"ndx_quote: {em_exc}")
            try:
                ndx_quote = yahoo_quote("^NDX", "纳指100")
            except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as yahoo_exc:
                errors.append(f"ndx_yahoo_quote: {yahoo_exc}")
                ndx_quote = {
                    "close": MARKET_SNAPSHOT["ndx_close"],
                    "prev_close": MARKET_SNAPSHOT["ndx_prev_close"],
                    "change_pct": (
                        (MARKET_SNAPSHOT["ndx_close"] - MARKET_SNAPSHOT["ndx_prev_close"])
                        / MARKET_SNAPSHOT["ndx_prev_close"]
                        * 100
                    ),
                    "date": MARKET_SNAPSHOT["market_date"],
                }

    try:
        spx_quote = sina_us_index_quote("gb_inx", "标普500")
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        errors.append(f"spx_sina_quote: {exc}")
        try:
            spx_quote = eastmoney_quote(MARKET_SYMBOLS["spx"]["secid"])
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as em_exc:
            errors.append(f"spx_quote: {em_exc}")
            try:
                spx_quote = yahoo_quote("^GSPC", "标普500")
            except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as yahoo_exc:
                errors.append(f"spx_yahoo_quote: {yahoo_exc}")
                spx_quote = {
                    "close": MARKET_SNAPSHOT["spx_close"],
                    "prev_close": MARKET_SNAPSHOT["spx_prev_close"],
                    "change_pct": (
                        (MARKET_SNAPSHOT["spx_close"] - MARKET_SNAPSHOT["spx_prev_close"])
                        / MARKET_SNAPSHOT["spx_prev_close"]
                        * 100
                    ),
                    "date": MARKET_SNAPSHOT["market_date"],
                }

    try:
        us10y_quote = eastmoney_quote(MARKET_SYMBOLS["us10y"]["secid"], scale=10000.0)
        us10y = as_float(us10y_quote["close"])
        us10y_date = str(us10y_quote.get("date") or "")
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"us10y_quote: {exc}")
        try:
            us10y_quote = cnbc_us10y_quote()
            us10y = as_float(us10y_quote["close"])
            us10y_date = str(us10y_quote.get("date") or "")
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as cnbc_exc:
            errors.append(f"us10y_cnbc_quote: {cnbc_exc}")
            us10y = MARKET_SNAPSHOT["us10y"]
            us10y_date = MARKET_SNAPSHOT["market_date"]

    try:
        ndx_rows = sina_us_klines(".NDX")
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"ndx_sina_klines: {exc}")
        try:
            ndx_rows = eastmoney_klines(MARKET_SYMBOLS["ndx"]["secid"])
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as em_exc:
            errors.append(f"ndx_klines: {em_exc}")
            try:
                ndx_rows = yahoo_klines("^NDX")
            except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as yahoo_exc:
                errors.append(f"ndx_yahoo_klines: {yahoo_exc}")

    try:
        spx_rows = sina_us_klines(".INX")
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"spx_sina_klines: {exc}")
        try:
            spx_rows = eastmoney_klines(MARKET_SYMBOLS["spx"]["secid"])
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as em_exc:
            errors.append(f"spx_klines: {em_exc}")
            try:
                spx_rows = yahoo_klines("^GSPC")
            except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError) as yahoo_exc:
                errors.append(f"spx_yahoo_klines: {yahoo_exc}")

    try:
        vix_rows = cboe_vix_history()
        vix_close = as_float(vix_rows[-1]["close"])
        vix_prev_close = as_float(vix_rows[-2]["close"])
        vix_date = str(vix_rows[-1]["date"])
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        errors.append(f"vix_history: {exc}")
        vix_close = MARKET_SNAPSHOT["vix_close"]
        vix_prev_close = MARKET_SNAPSHOT["vix_close"]
        vix_date = MARKET_SNAPSHOT["market_date"]

    try:
        pe_snapshot = build_nasdaq_pe_snapshot()
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        errors.append(f"nasdaq_pe: {exc}")
        pe_snapshot = dict(PE_SNAPSHOT)

    ndx_history = [as_float(row.get("close")) for row in ndx_rows[-252:]]
    spx_history = [as_float(row.get("close")) for row in spx_rows[-252:]]
    price_percentile = (
        percentile_rank(as_float(ndx_quote["close"]), ndx_history)
        + percentile_rank(as_float(spx_quote["close"]), spx_history)
    ) / 2
    valuation_percentile = as_float(pe_snapshot.get("nasdaq_pe_percentile"), price_percentile)

    ret20 = (trailing_return(ndx_rows, 20) + trailing_return(spx_rows, 20)) / 2
    ret60 = (trailing_return(ndx_rows, 60) + trailing_return(spx_rows, 60)) / 2
    valuation_score = clamp(1.0 + valuation_percentile * 8.5, 1.0, 9.5)
    volatility_score = clamp(9.5 - max(0.0, vix_close - 12.0) * 0.35, 1.0, 9.5)
    trend_score = clamp(5.0 + ret20 * 0.12 + ret60 * 0.06, 1.0, 9.5)
    rates_score = clamp((us10y - 3.0) * 1.8 + 4.0, 1.0, 9.5)
    composite = round(
        valuation_score * 0.38
        + volatility_score * 0.24
        + trend_score * 0.22
        + rates_score * 0.16,
        1,
    )
    values = {
        "valuation_percentile": valuation_percentile,
        "nasdaq_pe": as_float(pe_snapshot.get("nasdaq_pe"), 0.0),
        "vix": vix_close,
        "ret20": ret20,
        "ret60": ret60,
        "us10y": us10y,
    }
    market_date = str(ndx_quote.get("date") or spx_quote.get("date") or vix_date or MARKET_SNAPSHOT["market_date"])
    payload = {
        "market_date": market_date,
        "fetch_time": observed_fetch_time(),
        "ndx_close": as_float(ndx_quote["close"]),
        "ndx_prev_close": as_float(ndx_quote["prev_close"]),
        "ndx_change_pct": as_float(ndx_quote["change_pct"]),
        "ndx_date": str(ndx_quote.get("date") or market_date),
        "spx_close": as_float(spx_quote["close"]),
        "spx_prev_close": as_float(spx_quote["prev_close"]),
        "spx_change_pct": as_float(spx_quote["change_pct"]),
        "spx_date": str(spx_quote.get("date") or market_date),
        "vix_close": vix_close,
        "vix_prev_close": vix_prev_close,
        "vix_change_pct": (vix_close - vix_prev_close) / vix_prev_close * 100 if vix_prev_close else 0.0,
        "vix_date": vix_date,
        "us10y": us10y,
        "us10y_date": us10y_date,
        **pe_snapshot,
        "valuation_percentile": valuation_percentile,
        "valuation_fallback_percentile": price_percentile,
        "valuation_method": "nasdaq_pe_5y_percentile" if pe_snapshot else "index_price_252d_percentile",
        "composite": composite,
        "temperature": classify_temperature(composite),
        "indicator_details": [
            {"name": "估值位置", "score": valuation_score, "plain": score_plain("估值位置", valuation_score, values)},
            {"name": "波动水平", "score": volatility_score, "plain": score_plain("波动水平", volatility_score, values)},
            {"name": "趋势强弱", "score": trend_score, "plain": score_plain("趋势强弱", trend_score, values)},
            {"name": "利率环境", "score": rates_score, "plain": score_plain("利率环境", rates_score, values)},
        ],
        "data_sources": {
            "index_quotes": "Sina US quote / Eastmoney push2 / Yahoo chart",
            "index_history": "Sina US daily K / Eastmoney push2his / Yahoo chart",
            "vix": "Cboe VIX history",
            "us10y": "Eastmoney US10Y / CNBC US10Y",
            "nasdaq_pe": pe_snapshot.get("nasdaq_pe_source") if pe_snapshot else None,
        },
        "source_mode": "partial_fallback" if errors else "live",
        "errors": errors,
    }
    return cache_set(MARKET_CACHE, payload, MARKET_CACHE_TTL)


def classify_temperature(composite: float) -> str:
    if composite <= 2.5:
        return "极冷"
    if composite <= 4.0:
        return "偏冷"
    if composite < 6.2:
        return "中性"
    if composite < 7.8:
        return "偏热"
    return "过热"


def coefficient_for_temperature(temperature: str) -> float:
    return {
        "极冷": 2.0,
        "偏冷": 1.35,
        "中性": 1.0,
        "偏热": 0.45,
        "过热": 0.0,
    }.get(temperature, 1.0)


def deploy_label_for_rate(rate: float) -> str:
    if rate <= 0:
        return "暂停买入"
    if rate < 0.008:
        return "少量买入"
    if rate < 0.018:
        return "正常买入"
    if rate < 0.028:
        return "加速买入"
    return "强力买入"


def calculate_strategy(body: dict[str, object]) -> dict[str, object]:
    market_snapshot = build_market_snapshot(force_refresh=bool(body.get("refresh")))
    cash = max(0.0, as_float(body.get("investable_cash")))
    ndx_holdings = max(0.0, as_float(body.get("ndx_holdings")))
    spx_holdings = max(0.0, as_float(body.get("spx_holdings")))
    target_ndx = clamp(as_float(body.get("target_ndx_pct"), 70.0), 0.0, 100.0)
    target_spx = clamp(as_float(body.get("target_spx_pct"), 30.0), 0.0, 100.0)
    base_rate_pct = clamp(as_float(body.get("base_rate_pct"), 1.5), 0.0, 10.0)

    ndx_change_pct = as_float(market_snapshot.get("ndx_change_pct"))
    spx_change_pct = as_float(market_snapshot.get("spx_change_pct"))
    composite = as_float(market_snapshot.get("composite"), 5.0)
    temperature = str(market_snapshot.get("temperature") or classify_temperature(composite))
    temp_coef = coefficient_for_temperature(temperature)

    invested_total = ndx_holdings + spx_holdings
    total_assets = cash + invested_total
    invest_ratio = invested_total / total_assets * 100 if total_assets else 0.0
    ndx_ratio = ndx_holdings / total_assets * 100 if total_assets else 0.0
    spx_ratio = spx_holdings / total_assets * 100 if total_assets else 0.0
    cash_ratio = cash / total_assets * 100 if total_assets else 0.0
    ndx_split = round(ndx_holdings / invested_total * 100) if invested_total else round(target_ndx)
    spx_split = round(spx_holdings / invested_total * 100) if invested_total else round(target_spx)

    target_sum = target_ndx + target_spx
    if target_sum <= 0:
        ndx_weight = 0.0
        spx_weight = 0.0
    else:
        ndx_weight = target_ndx / target_sum
        spx_weight = target_spx / target_sum

    base_amount = cash * (base_rate_pct / 100.0)
    position_adjustment = clamp((90.0 - invest_ratio) / 90.0, 0.25, 1.15)
    deploy_rate = (base_rate_pct / 100.0) * temp_coef * position_adjustment
    deploy_amount = min(cash, round(cash * deploy_rate))

    ndx_target_value = total_assets * ndx_weight
    spx_target_value = total_assets * spx_weight
    ndx_gap = max(0.0, ndx_target_value - ndx_holdings)
    spx_gap = max(0.0, spx_target_value - spx_holdings)
    gap_sum = ndx_gap + spx_gap
    if deploy_amount <= 0:
        ndx_amount = 0
        spx_amount = 0
    elif gap_sum > 0:
        ndx_amount = round(deploy_amount * ndx_gap / gap_sum)
        spx_amount = int(deploy_amount - ndx_amount)
    else:
        ndx_amount = round(deploy_amount * ndx_weight)
        spx_amount = int(deploy_amount - ndx_amount)

    reasons = [
        f"综合温度评分 {composite:.1f}/10，当前判定为{temperature}。",
        f"纳指100今日 {ndx_change_pct:+.2f}%，标普500今日 {spx_change_pct:+.2f}%。",
        (
            f"纳指100 PE 为 {as_float(market_snapshot.get('nasdaq_pe')):.2f}，"
            f"近5年分位约 {as_float(market_snapshot.get('nasdaq_pe_percentile')) * 100:.0f}%。"
            if market_snapshot.get("nasdaq_pe")
            else "PE估值源暂不可用，估值位置回退为指数点位分位。"
        ),
        f"VIX 为 {as_float(market_snapshot.get('vix_close')):.2f}，波动越高代表市场越恐慌，温度越冷。",
        f"10 年期美债收益率 {as_float(market_snapshot.get('us10y')):.3f}%，用于衡量利率对权益估值的压力。",
        f"当前仓位约 {invest_ratio:.1f}%，本次按目标配置优先补足偏离较大的方向。",
    ]
    if deploy_amount == 0:
        judgment = "市场温度偏高或可投资现金不足，今日以观察为主。"
    elif temp_coef > 1:
        judgment = "市场偏冷，适合在预算内分批提高部署强度。"
    elif temp_coef < 1:
        judgment = "市场略偏热，建议降低投入强度并保持纪律。"
    else:
        judgment = "市场中性，按默认部署率和目标配置执行。"

    return {
        "success": True,
        "market_date": market_snapshot.get("market_date") or MARKET_SNAPSHOT["market_date"],
        "fetch_time": market_snapshot.get("fetch_time") or observed_fetch_time(),
        "market": {
            "ndx_close": market_snapshot.get("ndx_close"),
            "ndx_change_pct": ndx_change_pct,
            "ndx_date": market_snapshot.get("ndx_date"),
            "spx_close": market_snapshot.get("spx_close"),
            "spx_change_pct": spx_change_pct,
            "spx_date": market_snapshot.get("spx_date"),
            "vix_close": market_snapshot.get("vix_close"),
            "vix_change_pct": market_snapshot.get("vix_change_pct"),
            "vix_date": market_snapshot.get("vix_date"),
            "us10y": market_snapshot.get("us10y"),
            "us10y_date": market_snapshot.get("us10y_date"),
            "nasdaq_pe": market_snapshot.get("nasdaq_pe"),
            "nasdaq_pe_date": market_snapshot.get("nasdaq_pe_date"),
            "nasdaq_pe_percentile": market_snapshot.get("nasdaq_pe_percentile"),
            "nasdaq_pe_fair_low": market_snapshot.get("nasdaq_pe_fair_low"),
            "nasdaq_pe_fair_high": market_snapshot.get("nasdaq_pe_fair_high"),
            "nasdaq_pe_label": market_snapshot.get("nasdaq_pe_label"),
            "nasdaq_pe_url": market_snapshot.get("nasdaq_pe_url"),
            "valuation_method": market_snapshot.get("valuation_method"),
            "composite": composite,
            "temperature": temperature,
            "data_sources": market_snapshot.get("data_sources", {}),
            "source_mode": market_snapshot.get("source_mode", "live"),
            "errors": market_snapshot.get("errors", []),
        },
        "plan": {
            "base_rate": base_rate_pct / 100.0,
            "deploy_rate": deploy_rate,
            "deploy_label": deploy_label_for_rate(deploy_rate),
            "total": int(deploy_amount),
            "ndx_amount": int(ndx_amount),
            "spx_amount": int(spx_amount),
            "explanation": {
                "base_amount": round(base_amount),
                "temperature_coefficient": temp_coef,
                "position_coefficient": position_adjustment,
                "judgment": judgment,
                "reasons": reasons,
                "indicator_details": market_snapshot.get("indicator_details", []),
            },
        },
        "portfolio": {
            "invested_total": round(invested_total),
            "investable_cash": round(cash),
            "invest_ratio": invest_ratio,
            "ndx_split": ndx_split,
            "spx_split": spx_split,
            "ndx_ratio": ndx_ratio,
            "spx_ratio": spx_ratio,
            "cash_ratio": cash_ratio,
        },
    }


class NaviHandler(BaseHTTPRequestHandler):
    server_version = "Navi100Replica/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        self.serve_static(parsed.path)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.send_response(HTTPStatus.OK)
            self.send_cors_headers()
            self.end_headers()
            return
        self.serve_static(parsed.path, body=False)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_json({"success": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        self.handle_api_post(parsed.path)

    def log_message(self, fmt: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            market_snapshot = cache_get(MARKET_CACHE)
            if market_snapshot is None and isinstance(MARKET_CACHE.get("payload"), dict):
                market_snapshot = dict(MARKET_CACHE["payload"])
            if market_snapshot is None:
                market_snapshot = {
                    "market_date": MARKET_SNAPSHOT["market_date"],
                    "data_sources": {"snapshot": "local fallback"},
                    "errors": [],
                }
            self.send_json(
                {
                    "status": "ok",
                    "data_ok": True,
                    "access": "open",
                    "market_date": market_snapshot.get("market_date") or MARKET_SNAPSHOT["market_date"],
                    "fetch_time": observed_fetch_time(),
                    "data_sources": market_snapshot.get("data_sources", {}),
                    "errors": market_snapshot.get("errors", []),
                }
            )
            return

        if path == "/api/me":
            self.send_json({"success": True, "access": "open"})
            return

        if path == "/api/funds":
            force_refresh = (query.get("refresh") or [""])[0] in {"1", "true", "yes"}
            payload = load_live_funds_payload(force_refresh=force_refresh)
            records = list(payload.get("records", []))
            records = self.filter_funds(records, query)
            payload["records"] = records
            payload["count"] = len(records)
            payload["success"] = True
            self.send_json(payload)
            return

        self.send_json({"success": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)

    def handle_api_post(self, path: str) -> None:
        body = self.read_json_body()
        if body is None:
            self.send_json({"success": False, "error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/redeem":
            self.send_json({"success": True, "access": "open"})
            return

        if path == "/api/calculate":
            self.send_json(calculate_strategy(body))
            return

        self.send_json({"success": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)

    def filter_funds(self, records: list[object], query: dict[str, list[str]]) -> list[object]:
        filtered = [item for item in records if isinstance(item, dict)]
        primary_category = (query.get("primary_category") or [""])[0]
        trade_mode = (query.get("trade_mode") or [""])[0]
        family = (query.get("family") or [""])[0]
        core_only = (query.get("core_only") or [""])[0]

        if primary_category and primary_category != "all":
            filtered = [item for item in filtered if item.get("primary_category") == primary_category]
        if trade_mode:
            filtered = [item for item in filtered if item.get("trade_mode") == trade_mode]
        if family:
            filtered = [item for item in filtered if item.get("family") == family]
        if core_only in {"1", "true", "yes"}:
            filtered = [item for item in filtered if item.get("is_core_wide", True)]
        return filtered

    def serve_static(self, raw_path: str, body: bool = True) -> None:
        rel = unquote(raw_path).lstrip("/") or "index.html"
        candidate = (PUBLIC_DIR / rel).resolve()
        if PUBLIC_DIR not in candidate.parents and candidate != PUBLIC_DIR:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.exists() or not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        data = candidate.read_bytes() if body else b""
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix == ".html":
            mime = "text/html; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(candidate.stat().st_size))
        self.end_headers()
        if body:
            self.wfile.write(data)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8787"))
    server = ThreadingHTTPServer((host, port), NaviHandler)
    print(f"Navi100 local replica: http://{host}:{port}")
    print("Access mode: open")
    server.serve_forever()


if __name__ == "__main__":
    main()
