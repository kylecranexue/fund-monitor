# Navi100 Local Replica

本目录是 Navi100 CTF 的本地后端复刻版本。它保留抓取到的前端页面，并实现兼容接口：

- `POST /api/redeem`
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

可用本地激活码：

```text
LOCAL-DEMO
NAVI-LOCAL
NAVI100-CTF
CTF-UNLOCK
```

任何以 `CTF-` 开头的激活码也会在本地复刻服务里通过，方便演示。
`/api/health` 会复刻线上可见元数据，例如 `activation_code_count: 50` 与
`activation_store: "env"`；这只是接口兼容信息，不包含真实环境变量。

部署到公网时建议在平台环境变量里设置：

```text
ACTIVATION_CODES=你自己的激活码1,你自己的激活码2
ALLOW_CTF_PREFIX=0
```

## 验证

服务启动后运行：

```bash
python3 tools/smoke_test.py
```

预期会看到 `redeem`、`me`、`calculate`、`funds` 均通过。
如果部署后关闭了默认演示码，可以这样验证线上服务：

```bash
BASE_URL=https://你的域名 SMOKE_CODE=你的激活码 python3 tools/smoke_test.py
```

## 文件

- `server.py`: 本地兼容后端，无第三方依赖。
- `public/index.html`: 抓取到的原始前端页面副本。
- `work/funds.json`: 从公开 `/api/funds` 保存的基金数据样本。
- `work/index.html`: 抓取到的原始首页样本。

说明：本复刻服务只实现本地授权与兼容功能，不读取或恢复线上环境变量。
