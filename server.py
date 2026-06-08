#!/usr/bin/env python3
"""Local Navi100-compatible backend for the open-access replica.

It serves the captured frontend and a compatible API surface so the UI can be
exercised end to end without an activation gate.
"""

from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
FUNDS_FILE = ROOT / "work" / "funds.json"

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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def observed_fetch_time() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S")


def json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def load_funds_payload() -> dict[str, object]:
    with FUNDS_FILE.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["success"] = True
    return payload


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
    cash = max(0.0, as_float(body.get("investable_cash")))
    ndx_holdings = max(0.0, as_float(body.get("ndx_holdings")))
    spx_holdings = max(0.0, as_float(body.get("spx_holdings")))
    target_ndx = clamp(as_float(body.get("target_ndx_pct"), 70.0), 0.0, 100.0)
    target_spx = clamp(as_float(body.get("target_spx_pct"), 30.0), 0.0, 100.0)
    base_rate_pct = clamp(as_float(body.get("base_rate_pct"), 1.5), 0.0, 10.0)

    ndx_change_pct = (
        (MARKET_SNAPSHOT["ndx_close"] - MARKET_SNAPSHOT["ndx_prev_close"])
        / MARKET_SNAPSHOT["ndx_prev_close"]
        * 100
    )
    spx_change_pct = (
        (MARKET_SNAPSHOT["spx_close"] - MARKET_SNAPSHOT["spx_prev_close"])
        / MARKET_SNAPSHOT["spx_prev_close"]
        * 100
    )

    valuation_score = 6.6
    volatility_score = clamp((MARKET_SNAPSHOT["vix_close"] - 10) / 2.8, 1.0, 9.5)
    trend_score = clamp(5.2 + (ndx_change_pct * 0.6) + (spx_change_pct * 0.4), 1.0, 9.5)
    rates_score = clamp((MARKET_SNAPSHOT["us10y"] - 3.0) * 1.8 + 4.0, 1.0, 9.5)
    composite = round(
        valuation_score * 0.36
        + volatility_score * 0.24
        + trend_score * 0.22
        + rates_score * 0.18,
        1,
    )
    temperature = classify_temperature(composite)
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
        f"VIX 为 {MARKET_SNAPSHOT['vix_close']:.2f}，市场波动处在可接受区间。",
        f"10 年期美债收益率 {MARKET_SNAPSHOT['us10y']:.3f}%，对权益估值形成一定压制。",
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
        "market_date": MARKET_SNAPSHOT["market_date"],
        "fetch_time": MARKET_SNAPSHOT["fetch_time"],
        "market": {
            "ndx_close": MARKET_SNAPSHOT["ndx_close"],
            "ndx_change_pct": ndx_change_pct,
            "spx_close": MARKET_SNAPSHOT["spx_close"],
            "spx_change_pct": spx_change_pct,
            "vix_close": MARKET_SNAPSHOT["vix_close"],
            "us10y": MARKET_SNAPSHOT["us10y"],
            "composite": composite,
            "temperature": temperature,
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
                "judgment": judgment,
                "reasons": reasons,
                "indicator_details": [
                    {
                        "name": "估值位置",
                        "score": valuation_score,
                        "plain": "纳指与标普处在偏高区间，买入节奏不宜过急。",
                    },
                    {
                        "name": "波动水平",
                        "score": volatility_score,
                        "plain": "VIX 未进入恐慌区，适合按计划小步执行。",
                    },
                    {
                        "name": "趋势强弱",
                        "score": trend_score,
                        "plain": "近期涨跌互现，趋势信号偏中性。",
                    },
                    {
                        "name": "利率环境",
                        "score": rates_score,
                        "plain": "长端利率仍偏高，对成长资产估值不算友好。",
                    },
                ],
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
            self.end_headers()
            return
        self.serve_static(parsed.path, body=False)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_json({"success": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        self.handle_api_post(parsed.path)

    def log_message(self, fmt: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
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
            self.send_json(
                {
                    "status": "ok",
                    "data_ok": True,
                    "access": "open",
                    "market_date": MARKET_SNAPSHOT["market_date"],
                    "fetch_time": observed_fetch_time(),
                }
            )
            return

        if path == "/api/me":
            self.send_json({"success": True, "access": "open"})
            return

        if path == "/api/funds":
            payload = load_funds_payload()
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
