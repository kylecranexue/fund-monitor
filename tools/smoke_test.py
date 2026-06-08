#!/usr/bin/env python3
"""Smoke test for the Navi100 local replica."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8787").rstrip("/")


def request_json(
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    expect_status: int = 200,
) -> dict:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != expect_status:
                raise AssertionError(f"{path}: expected {expect_status}, got {resp.status}")
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code != expect_status:
            raise
        return json.load(exc)


def main() -> int:
    try:
        health = request_json("/api/health")
        assert health["status"] == "ok"
        assert health["access"] == "open"

        me = request_json("/api/me")
        assert me["success"] is True
        assert me["access"] == "open"

        calc = request_json(
            "/api/calculate",
            method="POST",
            body={
                "investable_cash": 200000,
                "ndx_holdings": 80000,
                "spx_holdings": 20000,
                "target_ndx_pct": 70,
                "target_spx_pct": 30,
                "base_rate_pct": 1.5,
            },
        )
        assert calc["success"] is True
        assert calc["plan"]["total"] > 0
        assert calc["market"]["temperature"]

        funds = request_json("/api/funds?primary_category=all&core_only=1&trade_mode=exchange&family=nasdaq")
        assert funds["success"] is True
        assert funds["count"] == len(funds["records"])
        assert funds["count"] > 0

    except (AssertionError, urllib.error.URLError, KeyError) as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        return 1

    print("health ok")
    print("open access ok")
    print(f"calculate ok: {calc['plan']['deploy_label']} {calc['plan']['total']}")
    print(f"funds ok: {funds['count']} filtered records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
