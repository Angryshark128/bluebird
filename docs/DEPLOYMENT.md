# 部署指南

青鸟 Bluebird 是纯标准库单文件服务，镜像约 25MB、运行内存 <50MB，任何能跑 Docker 的环境都能部署。按场景选择：

| 场景 | 方式 | 说明 |
| --- | --- | --- |
| 自己有服务器 | [Docker Compose](#docker-compose-自由服务器) | 推荐，数据可持久化 |
| 不想管服务器 | [Render](#render) | 免费层可用，磁盘临时 |
| 其他托管平台 | [Zeabur / Railway 等](#zeabur--railway-等托管平台) | 识别 Dockerfile 即可 |

## 通用要点（所有方式适用）

- **端口**：服务优先监听平台注入的 `PORT` 环境变量，其次 `NOTIFY_PORT`（默认 8082）
- **根路径部署（推荐）**：默认不挂子路径（子域名 / 独立端口直接使用），健康检查、Webhook、面板均在根路径
- **可选子路径**：设置 `BASE_PATH=bluebird` 可挂到 `/bluebird/` 下（健康检查、Webhook、面板均带此前缀）
- **健康检查路径**：`/health`（子路径部署为 `/<BASE_PATH>/health`）
- **Webhook URL**：`https://你的域名/hooks/<来源 ID>`（子路径部署为 `https://你的域名/bluebird/hooks/<来源 ID>`；地址在面板来源详情行复制，按 ID 生成故改名不受影响）
- **审计面板**：`https://你的域名/ui`（登录后使用；子路径部署为 `https://你的域名/bluebird/ui`）
- **环境变量**：见文末[完整变量表](#完整环境变量表)，必填为 `WEBHOOK_SECRET`、面板凭据 `NOTIFY_AUTH_USER` / `NOTIFY_AUTH_PASS`、以及至少一个渠道 key

## Docker Compose（自由服务器）

```bash
git clone https://github.com/Angryshark128/bluebird.git
cd bluebird
cp .env.example .env     # 填写必填项
docker compose up -d --build
curl http://127.0.0.1:8082/health   # {"ok": true}
```

### 反代（Nginx 示例）

**子域名根路径部署（推荐）**——所有路径透传，无需前缀处理：

```nginx
server {
    server_name bluebird.example.com;

    location / {
        proxy_pass http://127.0.0.1:8082;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**子路径部署**（`BASE_PATH=bluebird` 时）——`location /bluebird/` 无尾斜杠透传完整路径（服务端自剥前缀）：

```nginx
location /bluebird/ {
    proxy_pass http://127.0.0.1:8082;   # 无尾斜杠：透传完整路径（配合 BASE_PATH）
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

- 数据卷 `/opt/bluebird` 保存去重记录 / 审计日志 / 动态配置（来源、渠道、开关），**随容器持久化**（upgrade 不丢数据）
- 也可直接 `docker run`：

```bash
docker run -d --name bluebird -p 8082:8082 \
  -e WEBHOOK_SECRET=xxx -e BARK_KEY=xxx -e NOTIFY_OWNER=your-username \
  -e NOTIFY_AUTH_USER=admin -e NOTIFY_AUTH_PASS=xxx \
  -v bluebird-data:/opt/bluebird \
  ghcr.io/angryshark128/bluebird
```


## Render

1. 打开 <https://dashboard.render.com/new/web> → **Connect** 你的 GitHub 仓库（自动识别 Dockerfile）
2. **Health Check Path** 填：`/health`
3. 环境变量按[完整变量表](#完整环境变量表)填写（必填：`WEBHOOK_SECRET`、`BARK_KEY` 或 `FEISHU_WEBHOOK` 等、`NOTIFY_OWNER`、`NOTIFY_AUTH_USER`/`NOTIFY_AUTH_PASS`）
4. Deploy 后访问 `https://<你的应用>.onrender.com/ui`，Webhook URL 为 `https://<你的应用>.onrender.com/hooks/<来源 ID>`（面板来源详情里复制；若设了 `BASE_PATH`，各路径加对应前缀）

**注意（免费层）**：
- 实例休眠：长时间无请求会停止，首次访问有冷启动延迟（30 秒左右）；Render 面板的 health check 会周期性唤醒
- **磁盘是临时的**：重启 / 重新部署后 SQLite（审计日志、动态配置、去重记录）会清空。需要持久化请挂载 **Persistent Disk** 到 `/opt/bluebird`（付费功能）
- 仓库 push 新代码会自动触发 redeploy（可在 Settings 关闭自动部署）

## Zeabur / Railway 等托管平台

- 从 GitHub 导入本仓库，自动识别 Dockerfile
- 配置环境变量（同上表），平台分配端口由 `PORT` 自动适配
- 磁盘持久化：各平台挂载持久卷到 `/opt/bluebird` 即可保留审计数据

## 完整环境变量表

按用途分组；**认证**与至少一个**推送渠道**为必填，其余不填走默认。

### 认证（必填，fail-closed）

| 变量 | 默认 | 必填 | 说明 |
| --- | --- | --- | --- |
| `WEBHOOK_SECRET` | 空 | ✅ | Webhook 签名密钥，创建 GitHub App 时填同一值（`openssl rand -hex 32` 生成） |
| `NOTIFY_AUTH_USER` | 空 | ✅ | 面板登录用户名 |
| `NOTIFY_AUTH_PASS` | 空 | ✅ | 面板登录密码；**两者任一为空时面板 / API / 文档一律拒绝访问** |
| `UI_SESSION_SECRET` | 空 | 否 | 面板会话签名密钥；留空则首启随机生成并落库（一般无需设置） |

### 来源过滤

| 变量 | 默认 | 必填 | 说明 |
| --- | --- | --- | --- |
| `NOTIFY_OWNER` | 空 | 否 | 只处理该账号名下仓库事件（GitHub 来源）；留空 = 处理所有仓库 |
| `GENERIC_TOKEN` | 空 | 否 | 通用 Hook 源 token（启用 `/hooks/<来源 ID>`）；不配 = 该端点 401 |

### 推送渠道（至少配置一个）

| 变量 | 默认 | 必填 | 说明 |
| --- | --- | --- | --- |
| `BARK_URL` | `https://api.day.app` | 否 | Bark 服务地址 |
| `BARK_KEY` | 空 | 渠道 | Bark 推送 key |
| `BARK_SOURCE` | `github` | 否 | Bark 通知 subtitle 来源标识 |
| `FEISHU_WEBHOOK` | 空 | 渠道 | 飞书群机器人 webhook |
| `FEISHU_SECRET` | 空 | 否 | 飞书加签密钥（开启「签名校验」时必填） |
| `WECOM_WEBHOOK` | 空 | 渠道 | 企业微信群机器人 webhook |
| `PUSHDEER_URL` | `https://api2.pushdeer.com` | 否 | PushDeer 服务地址（自建时改） |
| `PUSHDEER_KEY` | 空 | 渠道 | PushDeer 推送 key（多个 key 用英文逗号分隔） |
| `GENERIC_WEBHOOK_URL` | 空 | 渠道 | 通用 Webhook 渠道地址（接收 POST JSON） |
| `GENERIC_WEBHOOK_SECRET` | 空 | 否 | 通用 Webhook 签名密钥（填了则请求带 `X-Bluebird-Signature-256`） |

### 监听与部署

| 变量 | 默认 | 必填 | 说明 |
| --- | --- | --- | --- |
| `NOTIFY_HOST` | `0.0.0.0` | 否 | 监听地址 |
| `PORT` / `NOTIFY_PORT` | `8082` | 否 | 监听端口（平台注入 `PORT` 自动生效） |
| `BASE_PATH` | 空（根路径） | 否 | 子路径前缀；填 `bluebird` 挂到 `/bluebird/` 下 |

### 行为调优（可选）

| 变量 | 默认 | 必填 | 说明 |
| --- | --- | --- | --- |
| `LOG_RETENTION_DAYS` | `30` | 否 | 审计日志保留天数 |
| `DEDUP_SECONDS` | `60` | 否 | 重复事件去重窗口（秒） |
| `UI_SESSION_HOURS` | `24` | 否 | 面板登录会话时长（小时） |
| `MAX_BODY_BYTES` | `1048576` | 否 | 单请求体上限（字节），超限返回 413 |
| `NOTIFY_TIMEOUT` | `30` | 否 | 单连接读写超时（秒），防慢连接 / 半开连接 |

渠道至少配置一个；**面板凭据必配**——`NOTIFY_AUTH_USER` / `NOTIFY_AUTH_PASS` 任一为空时，面板与 API 一律拒绝访问（fail-closed），不会退回免登录。GitHub 来源必须配 secret、通用来源必须配 token，否则 `/hooks/*` 返回 401。

## 验证清单

```bash
# 1. 健康检查
curl https://你的域名/health
# → {"ok": true}

# 2. 通用 Hook（无需 GitHub App；Authorization: Bearer）
curl -X POST https://你的域名/hooks/<来源 ID> \
  -H "Authorization: Bearer $GENERIC_TOKEN" \
  -d '{"title":"测试","body":"链路通了"}'
# → {"ok": true, "source": "generic", "pushed": true}

# 3. 面板
# 浏览器打开 /ui 登录后应能看到记录
```
