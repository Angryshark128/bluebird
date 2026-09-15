# 如何添加信息源与分发渠道

青鸟 Bluebird 由两层组成：

- **通知源**：事件的发送方。谁触发推送（GitHub 仓库事件 / 通用 HTTP 调用）
- **分发渠道**：通知的接收方。推给谁（Bark / 飞书 / 企业微信 / PushDeer / 通用 Webhook）

两类都可以添加**多个实例**，互相独立启停，推送时并行分发、单实例异常不影响其他。

## 进入设置

面板右上角点击 **齿轮图标**（设置）→ 打开设置弹窗，分三个页签：

- **通知源**：管理所有事件来源
- **分发渠道**：管理所有推送目标
- **通用**：数据保留天数、清空历史

每个页签顶部有搜索框和类型/状态过滤，列表项右侧为 **编辑**（铅笔）与 **删除**（垃圾桶）按钮，左侧开关用于临时启停。

---

## 添加通知源

### GitHub 信息源

1. 设置 → 通知源 → 点击「＋ 添加通知源」
2. 填写：
   - **名称**：字母/数字/`-_.`，如 `github`、`server-monitor`；仅作展示名，可随时改（Webhook 地址用的是来源 ID，不受改名影响）
   - **类型**：GitHub
   - **Webhook Secret**：与 GitHub 侧配置的 Secret 一致；也可点「随机生成 Secret」由服务端生成。**来源未配 secret 时一律拒绝请求（401）**
   - **事件类型**：默认全选。取消勾选的事件不推送；**全不选 = 该来源不推送任何事件**
   - **通知渠道**：默认全选（所有已启用渠道）。取消勾选即不推到对应渠道；**全不选 = 该来源不推送**
   - **通知本人操作**：默认忽略（你本人账号触发的 star/fork 等不通知，避免自扰）
3. 保存后，在列表行点击 **展开箭头** 查看该来源的 Webhook 地址
4. 到 GitHub 仓库 → `Settings → Webhooks → Add webhook`：
   - **Payload URL**：`https://<你的域名>/hooks/<来源 ID>`（在来源详情行点复制按钮取完整地址；用 ID 的好处是改名不会导致地址失效）
   - **Content type**：`application/json`
   - **Secret**：填面板里设置的 Webhook Secret
   - **Which events**：按需选择（建议 Select individual events：star / fork / Issues / Pull requests / Workflow runs）
   - 创建后点击 `Redeliver` 可立刻测试

### 通用信息源

适用于任何能发 HTTP POST 的场景（脚本、CI、监控系统等）：

1. 设置 → 通知源 →「＋ 添加通知源」
2. 填写：
   - **名称**：如 `server-monitor`、`demo-app`
   - **类型**：通用
   - **Token**：留空保存时自动生成；也可点「随机生成 Token」手动生成
3. 保存后把 Token 配置到调用方

**调用方式**（`Authorization: Bearer <Token>` 必填）：

```bash
curl -X POST https://<你的域名>/hooks/<来源 ID> \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <Token>" \
  -d '{"title": "构建成功", "body": "server-monitor 任务通过", "event": "build", "repo": "ops"}'
```

- JSON 字段：`title`（标题）、`body`（正文）、`event`（事件名，配合来源级事件白名单过滤）、`repo`（仓库名）
- 也支持纯文本：首行为标题，其余为正文

---

## 添加分发渠道

### Bark（iPhone 推送）

1. iPhone 安装 **Bark** App（App Store），打开后记录专属 **Key**（形如 `xxxxxxxxxxxxxxxx`）
2. 设置 → 分发渠道 →「＋ 添加渠道」：
   - **名称**：如 `MyIPhone`
   - **类型**：Bark
   - **Key**（必填）：App 里的推送 Key
   - **服务地址**：默认 `https://api.day.app`，自建 Bark 服务器时改
3. 保存。Bark 通知按**信息源分组**（每个来源在通知中心独立分组）

### 飞书（群机器人）

1. 飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人，复制 **Webhook 地址**
2. 若开启「签名校验」，复制 **签名密钥**
3. 设置 → 分发渠道 →「＋ 添加渠道」：类型选飞书，粘贴 Webhook 与密钥（可选）并保存

### 企业微信（群机器人）

1. 企业微信群 → 右上角 → 添加群机器人，复制 **Webhook 地址**（`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...`）
2. 设置 → 分发渠道 →「＋ 添加渠道」：类型选企业微信，粘贴 Webhook 并保存

### PushDeer（iOS / Android 推送）

1. 安装 **PushDeer** App 并登录，在 App 内取得专属 **Key**
2. 设置 → 分发渠道 →「＋ 添加渠道」：
   - **名称**：如 `MyPushDeer`
   - **类型**：PushDeer
   - **Key**（必填）：App 里的推送 Key；多个 Key 用英文逗号分隔可一次推给多台设备
   - **服务地址**：默认 `https://api2.pushdeer.com`，自建 PushDeer 服务时改成自己的地址
3. 保存。标题作为消息第一行、正文作为第二行推送

> 官方服务限速 60 次/分钟；该开源项目已停止维护，自建服务需自行维护推送证书。

### 通用 Webhook（自建服务 / 任意 HTTP 接收端）

适用于任何能接收 HTTP POST 的地址（如自己的通知机器人、n8n、Home Assistant 等）：

1. 设置 → 分发渠道 →「＋ 添加渠道」：
   - **名称**：如 `my-bot`
   - **类型**：通用 Webhook
   - **Webhook URL**（必填）：接收端地址
   - **签名密钥**（可选）：填了则每个请求附带头 `X-Bluebird-Signature-256: sha256=<HMAC-SHA256(请求体)>`，接收端用同一密钥可校验来源
2. 保存

请求体为 JSON（`Content-Type: application/json`）：

```json
{"title": "构建成功", "body": "server-monitor 任务通过", "source": "server-monitor"}
```

接收端返回 **2xx** 即视为成功（响应体不解析）；非 2xx、连接失败或超时记为失败。

---

## 验证

添加完成后，回到面板主页：

- 顶部 **统计趋势图**：按日查看推送量，可切换「按通知源 / 按分发渠道 / 按是否成功」，鼠标悬停查看每日明细
- 下方 **推送记录**：按来源/渠道/事件过滤，查看每条推送的成功失败状态
- 发一条测试推送（如 GitHub Webhook 的 Redeliver，或通用来源的 curl），确认手机/群能收到
- 或到「分发渠道」展开任意渠道，点 **发送测试推送** 直接验证该渠道配置是否可用（测试不写入推送记录）

## 常见问题

- **收不到通知**：查看推送记录里该条的状态是否为「失败」；检查对应渠道实例是否启用（开关）、来源的「通知渠道」是否勾选了该渠道
- **来源不推送**：检查「事件类型」是否全不选了（全不选 = 不推送）；通用来源检查 `Authorization: Bearer <Token>` 是否匹配
- **删除渠道后来源仍引用**：来源的「通知渠道」勾选里不会出现已删除渠道，保存一次即可清理
