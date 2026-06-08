# Navi100 源码交付说明

## 交付结论

本包提供 Navi100 的本地可运行源码复刻，覆盖前端页面与主要后端接口。该复刻基于公开前端、公开 `/api/funds` 数据、公开 `/api/health` 信息，以及前端 JavaScript 中暴露的接口契约完成。

未包含线上生产环境的非公开后端源码、环境变量或部署密钥。

## 已复刻接口

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/api/redeem` | 本地激活码兑换，返回兼容 token |
| `GET` | `/api/me` | token 校验，返回激活码状态 |
| `POST` | `/api/calculate` | 投资策略计算，返回市场、计划、组合与解释字段 |
| `GET` | `/api/funds` | 基金池数据，支持 `primary_category`、`trade_mode`、`family`、`core_only` 筛选 |
| `GET` | `/api/health` | 本地服务健康状态 |

## 前端契约来源

前端页面中暴露的关键调用点：

- `localStorage.navi_access_token`
- `POST /api/redeem`
- `GET /api/me`
- `POST /api/calculate`
- `GET /api/funds?...`
- `Authorization: Bearer <token>`

复刻后端按页面 `render(data)` 所需结构返回字段：

- `success`
- `market_date`
- `fetch_time`
- `market`
- `plan`
- `plan.explanation`
- `portfolio`

## 文件清单

| 文件 | 说明 |
|---|---|
| `server.py` | 本地兼容后端源码，无第三方依赖 |
| `public/index.html` | Navi100 前端页面副本 |
| `work/index.html` | 抓取到的原始首页样本 |
| `work/funds.json` | 抓取到的基金池数据样本 |
| `work/health.json` | 抓取到的健康检查样本 |
| `tools/smoke_test.py` | 自动验证脚本 |
| `README.md` | 运行说明 |

## 线上可见契约复核

2026-06-08 复核确认：

- 首页与 `work/index.html` SHA-256 一致：`53c81c2ba4fc9cd7cbcbffbd92a8983b0c330b1c307456490d0327b60c8743f6`
- `/api/funds?primary_category=all` 与 `work/funds.json` SHA-256 一致：`cb1355b72899aa419801456c6a229f4ce0e195425791efeb80c9294f86c19325`
- `/api/health` 暴露 `activation_code_count: 50`、`activation_store: "env"`、`token_ttl_days: 7`
- 未带 token 访问受限接口返回 `activation_required`，伪造 token 返回 `invalid_token`

## 运行方式

```bash
python3 server.py
```

访问：

```text
http://127.0.0.1:8787/
```

本地激活码：

```text
LOCAL-DEMO
NAVI-LOCAL
NAVI100-CTF
CTF-UNLOCK
```

## 验证方式

```bash
python3 tools/smoke_test.py
```

已验证：

- 激活成功
- token 校验成功
- 策略计算成功
- 基金表加载与筛选成功
- 原前端可正常渲染主要功能
