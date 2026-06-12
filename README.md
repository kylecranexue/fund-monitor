# Navi100 Local Replica

本目录是 Navi100 的开放访问复刻版本。它保留抓取到的前端页面，并实现兼容接口：

- `GET /api/me`
- `POST /api/calculate`
- `GET /api/funds`
- `GET /api/health`

当前版本不再只使用静态样本：

- 基金池优先联网同步东方财富/Sina 行情与估算数据，失败时回退 `work/funds.json`。
- 市场温度会实时拉取纳指、标普、VIX、美国 10 年期国债收益率、CNN Fear & Greed、纳指 PE 并重新计算；任一实时源失败时会使用缓存或本地快照，并在接口/页面上标明回退状态。
- 纳指 PE 免费源来自 World PE Ratio 的 QQQ 估算口径，默认按其历史月度 PE 序列计算近 10 年分位；如有理杏仁/Wind/雪球等专业口径，可用环境变量覆盖。
- 左侧买入强度会作为默认部署率参与策略计算，联动今日买入金额和纳指/标普分配。

## 运行

```bash
python3 server.py
```

打开：

```text
http://127.0.0.1:8787/
```

打开后无需激活码，可以直接刷新策略和查看基金池。

可选环境变量：

```bash
NAVI_UPSTREAM_BASE=https://www.navi100.top
FUNDS_CACHE_TTL_SECONDS=900
MARKET_CACHE_TTL_SECONDS=900
NAVI_HTTP_TIMEOUT_SECONDS=12
NASDAQ_PE_TTM=33.33
NASDAQ_PE_10Y_PERCENTILE=81.5
NASDAQ_PE_DATE=2026-06-10
NASDAQ_PE_SOURCE=Professional PE source
NASDAQ_PE_URL=https://example.com/source
```

`NASDAQ_PE_10Y_PERCENTILE` 支持 `81.5` 或 `0.815` 两种写法。配置后，页面会优先展示这组 PE-TTM 与十年分位；不配置时使用免费源估算值。

## 验证

服务启动后运行：

```bash
python3 tools/smoke_test.py
```

预期会看到 `me`、`calculate`、`funds` 均通过。
部署后可以这样验证线上服务：

```bash
BASE_URL=https://你的域名 python3 tools/smoke_test.py
```

## 文件

- `server.py`: 本地兼容后端，无第三方依赖，包含联网数据抓取、缓存和快照回退。
- `public/index.html`: 抓取到的原始前端页面副本。
- `work/funds.json`: 从公开 `/api/funds` 保存的基金数据样本。
- `work/index.html`: 抓取到的原始首页样本。

说明：本复刻服务为开放访问版本，不需要激活码。
