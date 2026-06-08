# Navi100 Local Replica

本目录是 Navi100 的开放访问复刻版本。它保留抓取到的前端页面，并实现兼容接口：

- `GET /api/me`
- `POST /api/calculate`
- `GET /api/funds`
- `GET /api/health`

## 运行

```bash
python3 server.py
```

打开：

```text
http://127.0.0.1:8787/
```

打开后无需激活码，可以直接刷新策略和查看基金池。

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

- `server.py`: 本地兼容后端，无第三方依赖。
- `public/index.html`: 抓取到的原始前端页面副本。
- `work/funds.json`: 从公开 `/api/funds` 保存的基金数据样本。
- `work/index.html`: 抓取到的原始首页样本。

说明：本复刻服务为开放访问版本，不需要激活码。
