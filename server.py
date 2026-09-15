#!/usr/bin/env python3
# 青鸟 Bluebird —— Webhook → Bark/飞书/企业微信 通知网关
# Python 标准库单文件：多来源 webhook + 签名校验 + 事件过滤去重 + 多渠道推送 + 审计
import base64
import contextlib
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("NOTIFY_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("NOTIFY_PORT") or "8082")
SECRET = os.environ.get("WEBHOOK_SECRET", "")
NOTIFY_OWNER = os.environ.get("NOTIFY_OWNER", "")
BARK_URL = os.environ.get("BARK_URL", "https://api.day.app").rstrip("/")
BARK_KEY = os.environ.get("BARK_KEY", "")
BARK_SOURCE = os.environ.get("BARK_SOURCE", "github")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
FEISHU_SECRET = os.environ.get("FEISHU_SECRET", "")
WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "")
PUSHDEER_URL = os.environ.get("PUSHDEER_URL", "https://api2.pushdeer.com").rstrip("/")
PUSHDEER_KEY = os.environ.get("PUSHDEER_KEY", "")
GENERIC_WEBHOOK_URL = os.environ.get("GENERIC_WEBHOOK_URL", "")
GENERIC_WEBHOOK_SECRET = os.environ.get("GENERIC_WEBHOOK_SECRET", "")
GENERIC_TOKEN = os.environ.get("GENERIC_TOKEN", "")
DB_PATH = os.environ.get("NOTIFY_DB", "/opt/bluebird/bluebird.db")
VERSION_FILE = os.path.join(os.path.dirname(DB_PATH), "version")
DEDUP_SECONDS = int(os.environ.get("DEDUP_SECONDS", "60"))
LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "30"))
UI_AUTH_USER = os.environ.get("NOTIFY_AUTH_USER", "")
UI_AUTH_PASS = os.environ.get("NOTIFY_AUTH_PASS", "")
UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
LOGIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login.html")
FAVICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.ico")
# 审计面板登录会话时长（小时）
UI_SESSION_HOURS = int(os.environ.get("UI_SESSION_HOURS", "24"))
# 面板会话签名密钥：独立于 WEBHOOK_SECRET；未配置时首启随机生成并持久化到数据库
UI_SESSION_SECRET = os.environ.get("UI_SESSION_SECRET", "")
# 单个请求体上限（字节，默认 1MB）：防止超大 body 占用内存与线程
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES") or 1024 * 1024)
# 单连接读写超时（秒，默认 30）：防止慢连接 / 半开连接长期占用线程
SOCKET_TIMEOUT = int(os.environ.get("NOTIFY_TIMEOUT") or 30)
# 子路径部署前缀（如 https://example.com/bluebird/hooks/github → "bluebird"）
# 默认根路径部署（子域名，如 https://bluebird.example.com/hooks/github）；需要子路径时显式设置
BASE_PATH = os.environ.get("BASE_PATH", "").strip("/")

# 机器人账号不通知（避免 dependabot 等刷屏），可扩展
BOT_LOGINS = {"dependabot[bot]", "renovate[bot]", "github-actions[bot]", "dependabot-preview[bot]"}

# 支持的 GitHub 来源事件（用于运行时启停开关）
EVENT_TYPES = ("watch", "fork", "issues", "issue_comment", "pull_request", "workflow_run")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bluebird")


@contextlib.contextmanager
def db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    with contextlib.suppress(OSError):
        os.chmod(DB_PATH, 0o600)   # 库内含渠道 key / token，收紧到仅服务进程可读
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS seen (delivery TEXT PRIMARY KEY, ts INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS push_log ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,"
                     "channel TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',"
                     "event_type TEXT NOT NULL DEFAULT '',"
                     "repo TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
                     "body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL)")
        try:
            conn.execute("ALTER TABLE push_log ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 旧库已存在 source 列
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        yield conn
        conn.commit()
    finally:
        conn.close()


def seen(delivery):
    """delivery 在去重窗口内出现过 → True（丢弃）；否则记录并返回 False。"""
    now = int(time.time())
    with db() as conn:
        conn.execute("DELETE FROM seen WHERE ts < ?", (now - DEDUP_SECONDS,))
        if conn.execute("SELECT 1 FROM seen WHERE delivery = ?", (delivery,)).fetchone():
            return True
        conn.execute("INSERT INTO seen(delivery, ts) VALUES(?, ?)", (delivery, now))
    return False


def log_push(channel, event_type, repo, title, body, status, ts=None, source=""):
    """记录一条推送审计日志，并清理过期记录。"""
    ts = int(time.time()) if ts is None else int(ts)
    with db() as conn:
        conn.execute(
            "INSERT INTO push_log(ts, channel, source, event_type, repo, title, body, status)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, channel, source, event_type, repo, title, body, status))
        conn.execute("DELETE FROM push_log WHERE ts < ?",
                     (ts - retention_days() * 86400,))


def _log_where(channel="", event_type="", days=7, source="", since=None):
    """构建审计日志查询的 WHERE 条件与参数（query_logs / count_logs 共用）。
    since：epoch 秒下限（ts >= since），与 days 条件可叠加；不传则行为不变。"""
    where, args = [], []
    if channel:
        where.append("channel = ?")
        args.append(channel)
    if event_type:
        where.append("event_type = ?")
        args.append(event_type)
    if source:
        where.append("source = ?")
        args.append(source)
    if days:
        where.append("ts >= ?")
        args.append(int(time.time()) - int(days) * 86400)
    if since is not None:
        where.append("ts >= ?")
        args.append(int(since))
    return where, args


def query_logs(channel="", event_type="", days=7, limit=100, source="", offset=0, since=None):
    """按条件查询审计日志，按时间倒序，支持 offset 分页。"""
    where, args = _log_where(channel, event_type, days, source, since)
    sql = ("SELECT id, ts, channel, source, event_type, repo, title, body, status"
           " FROM push_log")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    cols = ("id", "ts", "channel", "source", "event_type", "repo", "title", "body", "status")
    return [dict(zip(cols, r)) for r in rows]


def count_logs(channel="", event_type="", days=7, source="", since=None):
    """统计符合条件的总条数与成功/失败数（供分页与 KPI 使用）。"""
    where, args = _log_where(channel, event_type, days, source, since)
    sql = ("SELECT COUNT(*), "
           "COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0)"
           " FROM push_log")
    if where:
        sql += " WHERE " + " AND ".join(where)
    with db() as conn:
        row = conn.execute(sql, args).fetchone()
    return {"total": row[0], "ok": row[1], "error": row[0] - row[1]}


STAT_GROUPS = {"source": "source", "channel": "channel", "status": "status"}


def query_stats(days=30, group="source"):
    """按日聚合推送统计（日期轴为本地时区自然日，缺数据日期补 0）。
    group: source / channel / status，返回 {"labels": [日期...], "series": [{"key", "values"}...]}。"""
    col = STAT_GROUPS.get(group)
    if col is None:
        raise ValueError(f"unknown stats group: {group}")
    days = max(1, min(int(days), 3650))
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days - 1)   # 含今天共 days 个自然日
    cutoff = int(time.mktime(start.timetuple()))
    with db() as conn:
        rows = conn.execute(
            f"SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d, "
            f"CASE WHEN {col} = '' THEN '(空)' ELSE {col} END AS k, COUNT(*) AS n "
            f"FROM push_log WHERE ts >= ? GROUP BY d, k ORDER BY d", (cutoff,)).fetchall()
    acc = {}
    for d, k, n in rows:
        acc.setdefault(k, {})[d] = n
    # 日期轴：起始日（含）到今天（含）
    labels = []
    d = start
    while d <= today:
        labels.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    # status 分组固定输出 ok/error 两序列（失败可能为 0 也要显示）；其余分组动态
    keys = ["ok", "error"] if group == "status" else list(acc.keys())
    series = [{"key": k, "values": [acc.get(k, {}).get(d, 0) for d in labels]} for k in keys]
    return {"labels": labels, "series": series}


def clear_logs():
    """清空全部推送历史，返回删除条数。"""
    with db() as conn:
        cur = conn.execute("DELETE FROM push_log")
    return cur.rowcount


def get_settings():
    """读取全部运行设置（key -> value）。"""
    with db() as conn:
        return dict(conn.execute("SELECT key, value FROM settings").fetchall())


def set_setting(key, value):
    """写入运行设置（key 形如 event.<type> / channel.<name> / source.<name>）。"""
    with db() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def is_enabled(prefix, name, default="1"):
    """开关是否启用（未设置用 default）。"""
    return get_settings().get(f"{prefix}.{name}", default) != "0"


def retention_days():
    """日志保留天数：settings.retention.days 优先，未配置时用环境变量默认值。"""
    try:
        return max(1, min(int(get_settings().get("retention.days", LOG_RETENTION_DAYS)), 3650))
    except (TypeError, ValueError):
        return LOG_RETENTION_DAYS


# ---------- 动态配置模型（渠道 / 通知源实例，settings 表 JSON 存储；首次调用时从环境变量迁移） ----------

CHANNEL_TYPES = ("bark", "feishu", "wecom", "pushdeer", "webhook")
SOURCE_TYPES = ("github", "generic")


def _default_channels_from_env():
    """从环境变量构建初始渠道实例（兼容旧部署，一次性迁移）。"""
    lst = []
    if BARK_KEY:
        lst.append({"name": "bark", "type": "bark",
                    "config": {"url": BARK_URL, "key": BARK_KEY, "source": BARK_SOURCE}})
    if FEISHU_WEBHOOK:
        lst.append({"name": "feishu", "type": "feishu",
                    "config": {"webhook": FEISHU_WEBHOOK, "secret": FEISHU_SECRET}})
    if WECOM_WEBHOOK:
        lst.append({"name": "wecom", "type": "wecom",
                    "config": {"webhook": WECOM_WEBHOOK}})
    if PUSHDEER_KEY:
        lst.append({"name": "pushdeer", "type": "pushdeer",
                    "config": {"url": PUSHDEER_URL, "key": PUSHDEER_KEY}})
    if GENERIC_WEBHOOK_URL:
        lst.append({"name": "webhook", "type": "webhook",
                    "config": {"url": GENERIC_WEBHOOK_URL, "secret": GENERIC_WEBHOOK_SECRET}})
    return lst


def _default_sources_from_env():
    """从环境变量构建初始通知源实例（兼容旧部署，一次性迁移）。"""
    lst = [{"name": "github", "type": "github", "config": {"secret": SECRET}}]
    if GENERIC_TOKEN:
        lst.append({"name": "generic", "type": "generic", "config": {"token": GENERIC_TOKEN}})
    return lst


SOURCE_ID_BYTES = 9   # token_urlsafe(9) → 12 字符，URL 安全


def _new_source_ids(items):
    """给缺失 id 的来源补一个稳定 ID（懒迁移）；与已有 name/id 不冲突。返回是否有变更。"""
    taken = {v for it in items for v in (it.get("id"), it.get("name")) if v}
    changed = False
    for it in items:
        if not it.get("id"):
            while True:
                sid = secrets.token_urlsafe(SOURCE_ID_BYTES)
                if sid not in taken:
                    break
            it["id"] = sid
            taken.add(sid)
            changed = True
    return changed


def _load_list(key, fallback, ensure_source_id=False):
    """读取 JSON 实例列表；从未配置时用 fallback 生成并落库（懒迁移）。
    ensure_source_id：来源列表补全缺失的稳定 ID（/hooks/<ID> 不随改名失效）。"""
    raw = get_settings().get(key)
    if raw is None:
        lst = fallback()
        if ensure_source_id:
            _new_source_ids(lst)
        set_setting(key, json.dumps(lst, ensure_ascii=False))
        return lst
    try:
        lst = json.loads(raw)
    except ValueError:
        log.warning("配置 %s 损坏，按空列表处理", key)
        return []
    if ensure_source_id and isinstance(lst, list) and _new_source_ids(lst):
        set_setting(key, json.dumps(lst, ensure_ascii=False))
    return lst


def get_channels():
    """全部渠道实例（[{name,type,config}]）。"""
    return _load_list("channels.list", _default_channels_from_env)


def set_channels(lst):
    set_setting("channels.list", json.dumps(lst, ensure_ascii=False))


def get_sources():
    """全部通知源实例（[{id,name,type,config}]）。"""
    return _load_list("sources.list", _default_sources_from_env, ensure_source_id=True)


def set_sources(lst):
    set_setting("sources.list", json.dumps(lst, ensure_ascii=False))


def find_source(key):
    """按 ID 或名称定位来源：ID 优先（改名后调用方不受影响），其次名称（兼容历史地址）。"""
    items = get_sources()
    for s in items:
        if s.get("id") and s["id"] == key:
            return s
    for s in items:
        if s.get("name") == key:
            return s
    return None


# 实例名称将拼进 Webhook 路径（/hooks/<名称>），仅允许 URL 路径安全字符
INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def _name_error(name):
    """校验实例名称，返回错误信息或 None。"""
    if not name:
        return "名称必填"
    if len(name) > 32:
        return "名称不超过 32 字符"
    if not INSTANCE_NAME_RE.match(name):
        return "名称仅支持字母、数字、-、_、."
    return None


def rename_history(col, old, new):
    """实例改名后同步历史：推送日志、启停开关、来源渠道引用跟随新名称。"""
    with db() as conn:
        conn.execute(f"UPDATE push_log SET {col}=? WHERE {col}=?", (new, old))
        prefix = "channel" if col == "channel" else "source"
        conn.execute("UPDATE settings SET key=? WHERE key=?",
                     (f"{prefix}.{new}", f"{prefix}.{old}"))
    if col == "channel":
        # 通知源实例 config.channels 里引用的旧渠道名一并更新
        items = get_sources()
        changed = False
        for s in items:
            chs = (s.get("config") or {}).get("channels")
            if chs and old in chs:
                s.setdefault("config", {})["channels"] = [new if c == old else c for c in chs]
                changed = True
        if changed:
            set_sources(items)


def channel_error(item):
    """校验渠道实例，返回错误信息或 None。"""
    name = str(item.get("name") or "").strip()
    typ = str(item.get("type") or "")
    cfg = item.get("config") or {}
    err = _name_error(name)
    if err:
        return err
    if typ not in CHANNEL_TYPES:
        return f"未知渠道类型: {typ}"
    if typ == "bark":
        if not cfg.get("key"):
            return "Bark 需要填写 Key"
    elif typ == "pushdeer":
        if not cfg.get("key"):
            return "PushDeer 需要填写 Key"
    elif typ == "webhook":
        if not cfg.get("url"):
            return "通用 Webhook 需要填写 URL"
    else:
        if not cfg.get("webhook"):
            return "需要填写 Webhook 地址"
    return None


def source_error(item):
    """校验通知源实例，返回错误信息或 None。"""
    name = str(item.get("name") or "").strip()
    typ = str(item.get("type") or "")
    cfg = item.get("config") or {}
    err = _name_error(name)
    if err:
        return err
    if typ not in SOURCE_TYPES:
        return f"未知通知源类型: {typ}"
    for k in ("channels", "events"):
        v = cfg.get(k)
        if v is not None and not isinstance(v, list):
            return f"{k} 必须是列表"
    return None


def verify(signature, body, secret=SECRET):
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _post_json(url, payload):
    """POST JSON 并解析响应（Bark/飞书/企业微信通用）。"""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_form(url, payload):
    """POST form-encoded 并解析响应（PushDeer 用 form 参数，不用 JSON）。"""
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_raw(url, body, headers):
    """POST 原始 body，返回响应是否 2xx（通用 Webhook：响应体不解析，任意内容都算成功）。"""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return 200 <= r.status < 300


def _push_channel(typ, title, body, cfg, source=""):
    """按渠道类型与实例配置推送；返回是否成功。"""
    if typ == "bark":
        key = cfg.get("key", "")
        if not key:
            return False
        payload = {"title": title, "body": body,
                   "subtitle": cfg.get("source", ""), "group": source or "github", "sound": "default"}
        url = (cfg.get("url") or BARK_URL).rstrip("/") + "/" + key
        res = _post_json(url, payload)
        ok = isinstance(res, dict) and res.get("code") == 200
        if not ok:
            log.warning("Bark 返回异常: %s", res)
        return ok

    if typ == "feishu":
        webhook = cfg.get("webhook", "")
        if not webhook:
            return False
        payload = {"msg_type": "text", "content": {"text": f"{title}\n{body}"}}
        secret = cfg.get("secret", "")
        if secret:
            timestamp = str(int(time.time()))
            sign = base64.b64encode(
                hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
            ).decode("utf-8")
            payload.update(timestamp=timestamp, sign=sign)
        res = _post_json(webhook, payload)
        ok = isinstance(res, dict) and (res.get("code") == 0 or res.get("StatusCode") == 0)
        if not ok:
            log.warning("飞书返回异常: %s", res)
        return ok

    if typ == "wecom":
        webhook = cfg.get("webhook", "")
        if not webhook:
            return False
        payload = {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
        res = _post_json(webhook, payload)
        ok = isinstance(res, dict) and res.get("errcode") == 0
        if not ok:
            log.warning("企业微信返回异常: %s", res)
        return ok

    if typ == "pushdeer":
        key = cfg.get("key", "")
        if not key:
            return False
        # 失败时 HTTP 仍是 200，只能靠 code 判断（0 = 成功）
        url = (cfg.get("url") or PUSHDEER_URL).rstrip("/") + "/message/push"
        res = _post_form(url, {"pushkey": key, "text": title, "desp": body, "type": "markdown"})
        ok = isinstance(res, dict) and res.get("code") == 0
        if not ok:
            log.warning("PushDeer 返回异常: %s", res)
        return ok

    if typ == "webhook":
        url = cfg.get("url", "")
        if not url:
            return False
        body_bytes = json.dumps({"title": title, "body": body, "source": source},
                                ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = cfg.get("secret", "")
        if secret:
            # 接收方按同样方式计算并对齐即可校验来源（hex，形如 sha256=<digest>）
            headers["X-Bluebird-Signature-256"] = "sha256=" + hmac.new(
                secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        ok = _post_raw(url, body_bytes, headers)
        if not ok:
            log.warning("通用 Webhook 返回异常: %s", url)
        return ok

    log.warning("未知渠道类型: %s", typ)
    return False


def notify(title, body, meta=None):
    """按来源配置推送到指定渠道并记录审计日志；任一成功即 True，单个渠道异常不影响其他。
    来源未配置 channels 时推送到全部已启用渠道；全局渠道开关仍生效。"""
    pushed = False
    meta = meta or {}
    event_type = meta.get("event", "")
    repo = meta.get("repo", "")
    source = meta.get("source", "")
    allowed = None
    if source:
        for s in get_sources():
            if s["name"] == source:
                chans = (s.get("config") or {}).get("channels")
                # 显式配置 channels 后以此为准：空列表 = 该来源不推送任何渠道；
                # 未配置（None）时推送到全部已启用渠道（旧配置兼容）
                allowed = set(chans) if chans is not None else None
                break
    for ch in get_channels():
        name = ch["name"]
        if allowed is not None and name not in allowed:
            continue
        if not is_enabled("channel", name):
            continue
        try:
            ok = _push_channel(ch["type"], title, body, ch.get("config") or {}, source)
            status = "ok" if ok else "error"
        except Exception as e:
            log.error("渠道 %s 推送异常: %s (%s)", name, title, e)
            ok, status = False, "error"
        log_push(name, event_type, repo, title, body, status, source=source)
        if ok:
            pushed = True
            log.info("渠道 %s 推送成功: %s", name, title)
    return pushed


def channel_test(name):
    """向指定渠道实例发一条测试推送，返回 (是否成功, 错误信息)。
    仅供面板「测试推送」使用：不写推送日志、不影响统计。"""
    ch = next((c for c in get_channels() if c["name"] == name), None)
    if ch is None:
        return False, "渠道不存在"
    try:
        ok = _push_channel(ch.get("type", ""), "青鸟 Bluebird 测试推送",
                           f"渠道「{name}」配置测试，收到即表示该通道可用。",
                           ch.get("config") or {}, "test")
    except Exception as e:
        log.warning("渠道 %s 测试推送异常: %s", name, e)
        return False, f"推送异常：{e}"
    return (True, "") if ok else (False, "渠道返回失败，详见服务端日志")


def repo_name(payload):
    return (payload.get("repository") or {}).get("name") or "unknown"


def repo_owner(payload):
    repo = payload.get("repository") or {}
    return (repo.get("owner") or {}).get("login") or (repo.get("full_name") or "/").split("/")[0]


def ignore_actor(payload, self_notify=None):
    """机器人操作始终过滤；本人操作默认过滤。
    self_notify 为 None 时回退全局 notify.self 开关（向后兼容），否则用来源级配置。"""
    actor = ((payload.get("sender") or {}).get("login") or "").lower()
    if actor in BOT_LOGINS:
        return True
    if actor and actor == NOTIFY_OWNER.lower():
        if self_notify is None:
            self_notify = is_enabled("notify", "self", default="0")
        return not self_notify
    return False


# ---------- 来源层（类型 = 一对 verify/handle，注册到 SOURCES；实例配置来自动态配置） ----------

def _verify_github(headers, body, cfg=None):
    """GitHub HMAC-SHA256 校验；实例未配 secret 时一律拒绝（fail-closed，避免无认证注入）。"""
    secret = (cfg or {}).get("secret", "")
    if not secret:
        return False
    return verify(headers.get("X-Hub-Signature-256", ""), body, secret)


def _generic_token(headers):
    """提取通用来源 token：标准 Authorization 头（Bearer <token>，兼容裸 token）。"""
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):]
    if auth and not auth.startswith("Basic "):
        return auth
    return ""


def _verify_generic(headers, body, cfg=None):
    """通用来源 token 校验（Authorization: Bearer <token>）；未配 token 时拒绝。"""
    token = (cfg or {}).get("token", "")
    return bool(token) and hmac.compare_digest(_generic_token(headers), token)


def _handle_github(payload, event, cfg=None):
    """GitHub 事件 → (title, body, meta)；忽略返回 None。
    cfg.events（来源级事件白名单）显式配置后以此为准：空列表 = 该来源不推送任何事件；
    未配置（None）时回退全局 event 开关。"""
    if not payload or (NOTIFY_OWNER and repo_owner(payload) != NOTIFY_OWNER):
        return None
    events_cfg = (cfg or {}).get("events")
    if events_cfg is not None:
        if event not in events_cfg:
            return None
    elif event in EVENT_TYPES and not is_enabled("event", event):
        return None
    # 本人操作通知：来源级 self 配置优先，未配置回退全局 notify.self
    self_notify = None if cfg is None else bool(cfg.get("self"))

    if event == "watch":
        if payload.get("action") != "started" or ignore_actor(payload, self_notify):
            return None
        actor = (payload.get("sender") or {}).get("login", "")
        repo = repo_name(payload)
        stars = (payload.get("repository") or {}).get("stargazers_count", "?")
        return (f"⭐ {actor} star 了 {repo}", f"{repo} 被 {actor} star，共 {stars} 星",
                {"event": "watch", "repo": repo})

    if event == "fork":
        if ignore_actor(payload, self_notify):
            return None
        actor = (payload.get("sender") or {}).get("login", "")
        repo = repo_name(payload)
        return (f"🍴 {actor} fork 了 {repo}", f"{actor} fork 了 {repo}",
                {"event": "fork", "repo": repo})

    if event == "issues":
        if payload.get("action") != "opened" or ignore_actor(payload, self_notify):
            return None
        actor = (payload.get("sender") or {}).get("login", "")
        repo = repo_name(payload)
        issue = payload.get("issue") or {}
        n = issue.get("number", "?")
        return (f"🐛 新 issue #{n}：{issue.get('title', '')}",
                f"{actor} 在 {repo} 提了 issue #{n}",
                {"event": "issues", "repo": repo})

    if event == "issue_comment":
        if payload.get("action") != "created" or ignore_actor(payload, self_notify):
            return None
        actor = (payload.get("sender") or {}).get("login", "")
        repo = repo_name(payload)
        issue = payload.get("issue") or {}
        n = issue.get("number", "?")
        kind = "PR" if "pull_request" in issue else "issue"
        comment = (payload.get("comment") or {}).get("body", "")[:80]
        return (f"💬 {actor} 评论了 {kind} #{n}",
                f"{repo} 的 {kind} #{n} 有新评论：{comment}",
                {"event": "issue_comment", "repo": repo})

    if event == "pull_request":
        if payload.get("action") != "opened" or ignore_actor(payload, self_notify):
            return None
        actor = (payload.get("sender") or {}).get("login", "")
        repo = repo_name(payload)
        pr = payload.get("pull_request") or {}
        n = pr.get("number", "?")
        return (f"🔀 新 PR #{n}：{pr.get('title', '')}",
                f"{actor} 向 {repo} 提交了 PR #{n}",
                {"event": "pull_request", "repo": repo})

    if event == "workflow_run":
        run = payload.get("workflow_run") or {}
        if payload.get("action") != "completed" or run.get("status") != "completed":
            return None
        conclusion = run.get("conclusion", "")
        if conclusion in ("skipped", "neutral"):
            return None
        repo = repo_name(payload)
        wf = payload.get("workflow") or {}
        wf_name = (wf.get("name") if isinstance(wf, dict) else wf) or "workflow"
        mark = {"success": "✅ CI 通过", "failure": "❌ CI 失败", "cancelled": "⏹ CI 取消",
                "timed_out": "⏰ CI 超时", "action_required": "⚠️ CI 待处理",
                "stale": "❌ CI 过期", "startup_failure": "❌ CI 启动失败"}.get(conclusion, "❓ CI")
        branch = run.get("head_branch", "")
        display = run.get("display_title", "") or ""
        body = f"{repo} · {branch}".strip(" ·")
        if display and display != branch:
            body += f" · {display}"
        return (f"{mark}：{wf_name}", body, {"event": "workflow_run", "repo": repo})

    return None


def _handle_generic(payload, event="", cfg=None):
    """通用来源 → (title, body, meta)；忽略返回 None。
    JSON: {"title","body","event","repo"}；纯文本：首行为标题。
    cfg.events 显式配置后以此为准：空列表 = 该来源不推送任何事件；
    未配置（None）时不按事件过滤。"""
    events_cfg = (cfg or {}).get("events")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        if events_cfg is not None and "generic" not in events_cfg:
            return None
        return (text.splitlines()[0][:60], text, {"event": "generic", "repo": ""})
    if isinstance(payload, dict):
        title = str(payload.get("title") or payload.get("message") or "").strip()
        body = str(payload.get("body") or payload.get("content") or "").strip()
        if not title and not body:
            return None
        ev = str(payload.get("event") or "generic")
        if events_cfg is not None and ev not in events_cfg:
            return None
        return (title or "通知", body,
                {"event": ev, "repo": str(payload.get("repo") or "")})
    return None


SOURCES = {
    "github": {"verify": _verify_github, "handle": _handle_github},
    "generic": {"verify": _verify_generic, "handle": _handle_generic},
}


def read_version():
    """当前部署版本：优先读数据目录 version 文件（CI 打 tag 部署时写入），否则环境变量，默认 dev。"""
    try:
        with open(VERSION_FILE) as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    return os.environ.get("NOTIFY_VERSION", "dev")


SESSION_SECRET_KEY = "session.secret"
_session_secret_cache = None


def session_secret():
    """面板会话签名密钥：环境变量优先，否则首启随机生成并持久化（重启不失效）。
    绝不回退到可预测常量，否则任何人都能伪造登录态。"""
    global _session_secret_cache
    if UI_SESSION_SECRET:
        return UI_SESSION_SECRET
    if _session_secret_cache:
        return _session_secret_cache
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (SESSION_SECRET_KEY,)).fetchone()
        if not row or not row[0]:
            row = (secrets.token_urlsafe(32),)
            conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (SESSION_SECRET_KEY, row[0]))
    _session_secret_cache = row[0]
    return _session_secret_cache


def ui_token(user):
    """签发审计会话 token（HMAC 签名，防篡改）。"""
    exp = int(time.time()) + UI_SESSION_HOURS * 3600
    payload = f"{user}.{exp}"
    sig = hmac.new(session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def ui_token_ok(token):
    """校验会话 token：签名有效、未过期、用户匹配。"""
    try:
        user, exp, sig = token.rsplit(".", 2)
        expect = hmac.new(session_secret().encode(), f"{user}.{exp}".encode(),
                          hashlib.sha256).hexdigest()
        return (hmac.compare_digest(sig, expect) and user == UI_AUTH_USER
                and int(exp) >= int(time.time()))
    except Exception:
        return False


def _abs(path):
    """构造含 BASE_PATH 前缀的完整路径。"""
    return "/" + (BASE_PATH + "/" if BASE_PATH else "") + path.lstrip("/")


def auth_ok(handler):
    """审计面板认证：签名 cookie 优先，兼容 Basic Auth。
    未配置凭据（用户名或密码为空）时一律拒绝（fail-closed）——面板能读到全部渠道 key/token，
    默认开放等于把密钥挂在公网上。"""
    if not (UI_AUTH_USER and UI_AUTH_PASS):
        return False
    cookies = handler.headers.get("Cookie", "")
    for part in cookies.split("; "):
        if part.startswith("bb_token=") and ui_token_ok(part[len("bb_token="):]):
            return True
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user, UI_AUTH_USER) and hmac.compare_digest(pwd, UI_AUTH_PASS)


# 安全响应头：面板是单页应用（内联脚本/样式 + Google Fonts），故 script/style 放开 inline
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class BodyError(Exception):
    """请求体不合法：Content-Length 非法 / 超限 / 分块传输。"""

    def __init__(self, status, error):
        super().__init__(error)
        self.status = status
        self.error = error


class Handler(BaseHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT   # 慢连接 / 半开连接不长期占用线程

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # 统一补安全响应头，各响应分支无需重复设置
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    @staticmethod
    def route(path):
        """规范化请求路径：去掉 BASE_PATH 前缀后返回内部路径；不在子路径下返回 None。"""
        path = path.rstrip("/") or "/"
        if BASE_PATH:
            prefix = "/" + BASE_PATH
            if path == prefix:
                return "/"
            if path.startswith(prefix + "/"):
                return path[len(prefix):]
            return None
        return path

    def _is_https(self):
        """是否经 HTTPS 反代访问（决定登录 Cookie 是否带 Secure）。"""
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _same_origin(self):
        """CSRF 防护：带 Origin/Referer 的写请求需与 Host 同源；
        两者都没有（脚本 / curl / webhook 调用方）时放行。"""
        src = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not src:
            return True
        return (urllib.parse.urlparse(src).netloc.lower()
                == (self.headers.get("Host") or "").lower())

    def _read_body(self):
        """读取请求体：拒绝非法 / 超限 Content-Length 与分块传输。
        拒绝时 body 未消费，一并关闭连接，避免残留字节被当成下一个请求解析。"""
        if (self.headers.get("Transfer-Encoding") or "").strip():
            self.close_connection = True
            raise BodyError(411, "chunked request body not supported")
        try:
            n = int((self.headers.get("Content-Length") or "0").strip())
        except ValueError:
            self.close_connection = True
            raise BodyError(400, "bad Content-Length")
        if n < 0:
            self.close_connection = True
            raise BodyError(400, "bad Content-Length")
        if n > MAX_BODY_BYTES:
            self.close_connection = True
            raise BodyError(413, f"body too large (limit {MAX_BODY_BYTES} bytes)")
        return self.rfile.read(n) if n else b""

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self):
        if not (UI_AUTH_USER and UI_AUTH_PASS):
            return self._json(503, {"ok": False, "error": (
                "面板未配置登录凭据（NOTIFY_AUTH_USER / NOTIFY_AUTH_PASS），已拒绝访问")})
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="bluebird"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = self.route(parsed.path)
        if path is None:
            return self._json(404, {"ok": False, "error": "not found"})

        # 根路径（子域名部署）：未登录跳登录、已登录跳面板
        if path == "/":
            return self._redirect(_abs("ui"))

        if path == "/health":
            return self._json(200, {"ok": True})

        if path == "/favicon.ico":
            try:
                with open(FAVICON_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(404, {"ok": False, "error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", "image/x-icon")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        if path == "/login":
            if not (UI_AUTH_USER and UI_AUTH_PASS):
                return self._json(503, {"ok": False, "error": (
                    "面板未配置登录凭据（NOTIFY_AUTH_USER / NOTIFY_AUTH_PASS），已禁用面板访问")})
            if auth_ok(self):
                return self._redirect(_abs("ui"))
            try:
                with open(LOGIN_FILE, "rb") as f:
                    html = f.read()
            except OSError:
                return self._json(500, {"ok": False, "error": "login.html missing"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            return self.wfile.write(html)

        if path == "/ui":
            if not auth_ok(self):
                return self._redirect(_abs("login"))
            try:
                with open(UI_FILE, "rb") as f:
                    html = f.read()
            except OSError:
                return self._json(500, {"ok": False, "error": "ui.html missing"})
            html = html.replace(b"__BB_VERSION__", read_version().encode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            return self.wfile.write(html)

        if path == "/help":
            # 帮助已并入 ui.html 的 #/help 视图：/help 保留 302 兼容旧书签，不再读 help.html
            if not auth_ok(self):
                return self._redirect(_abs("login"))
            return self._redirect(_abs("ui#/help"))

        if path.startswith("/api/logs"):
            if not auth_ok(self):
                return self._unauthorized()
            q = urllib.parse.parse_qs(parsed.query)

            def g(key, default):
                try:
                    return (q.get(key) or [default])[0]
                except Exception:
                    return default

            def gint(key, default):
                try:
                    return int(g(key, str(default)))
                except (TypeError, ValueError):
                    return default

            channel, event_type, days, source = (g("channel", ""), g("event_type", ""),
                                                 g("days", "7"), g("source", ""))
            raw_since = g("since", "").strip()
            try:
                since = int(raw_since) if raw_since else None
            except ValueError:
                since = None   # 非法的 since 视为未传，保持向后兼容
            page = max(1, gint("page", 1))
            page_size = min(max(gint("page_size", gint("limit", 100)), 1), 500)
            counts = count_logs(channel=channel, event_type=event_type,
                                days=days, source=source, since=since)
            items = query_logs(channel=channel, event_type=event_type,
                               days=days, limit=page_size,
                               source=source, offset=(page - 1) * page_size,
                               since=since)
            return self._json(200, {"ok": True, "items": items, "total": counts["total"],
                                    "ok_count": counts["ok"], "error_count": counts["error"],
                                    "page": page, "page_size": page_size})

        if path.startswith("/api/stats"):
            if not auth_ok(self):
                return self._unauthorized()
            q = urllib.parse.parse_qs(parsed.query)

            def g2(key, default):
                try:
                    return (q.get(key) or [default])[0]
                except Exception:
                    return default

            group = g2("group", "source")
            if group not in STAT_GROUPS:
                return self._json(400, {"ok": False, "error": f"unknown group: {group}"})
            raw_days = g2("days", "")
            try:
                days = int(raw_days) if raw_days else retention_days()
            except ValueError:
                days = retention_days()
            stats = query_stats(days=days, group=group)
            return self._json(200, {"ok": True, "group": group, "days": days, **stats})

        if path == "/api/version":
            if not auth_ok(self):
                return self._unauthorized()
            return self._json(200, {"ok": True, "version": read_version()})

        if path.startswith("/docs/"):
            if not auth_ok(self):
                return self._unauthorized()
            fname = urllib.parse.unquote(path[len("/docs/"):])
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", fname):
                return self._json(404, {"ok": False, "error": "not found"})
            fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
            except OSError:
                return self._json(404, {"ok": False, "error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        if path.startswith("/api/settings"):
            if not auth_ok(self):
                return self._unauthorized()
            s = get_settings()
            return self._json(200, {
                "ok": True,
                "sources": {i["name"]: is_enabled("source", i["name"]) for i in get_sources()},
                "events": {e: s.get(f"event.{e}", "1") != "0" for e in EVENT_TYPES},
                "channels": {i["name"]: is_enabled("channel", i["name"]) for i in get_channels()},
                "notify": {"self": is_enabled("notify", "self", default="0")},
                "retention": {"days": retention_days()},
                "source_items": get_sources(),
                "channel_items": get_channels(),
            })

        return self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = self.route(parsed.path)
        if path is None:
            return self._json(404, {"ok": False, "error": "not found"})
        if path == "/api/settings":
            if not auth_ok(self):
                return self._unauthorized()
            if not self._same_origin():
                return self._json(403, {"ok": False, "error": "cross-origin request rejected"})
            try:
                data = json.loads(self._read_body().decode("utf-8") or "{}")
            except BodyError as e:
                return self._json(e.status, {"ok": False, "error": e.error})
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"ok": False, "error": "bad json"})
            key = data.get("key", "")
            prefix, _, name = key.partition(".")

            def _save_item(loader, saver, validator, action_name, col):
                item = data.get("item") or {}
                err = validator(item)
                if err:
                    return self._json(400, {"ok": False, "error": err})
                n = str(item["name"]).strip()
                old = str(data.get("original_name") or "").strip()
                # 改名 / 编辑时保留原条目的稳定 ID：旧的 /hooks/<ID> 地址继续可用
                prev = next((c for c in loader() if c["name"] == (old or n)), None)
                if prev and prev.get("id") and not item.get("id"):
                    item["id"] = prev["id"]
                # 移除与新名称或原名称（编辑改名时）相同的旧条目，避免"编辑变新增"
                cur = [c for c in loader() if c["name"] != n and c["name"] != old]
                saver(cur + [item])
                if old and old != n:
                    rename_history(col, old, n)
                log.info("%s %s 保存（%s）", action_name, n, self.client_address[0])
                return self._json(200, {"ok": True, "item": item})

            if action := data.get("action", ""):
                if action == "channel/save":
                    return _save_item(get_channels, set_channels, channel_error, "渠道", "channel")
                if action == "channel/test":
                    n = str(data.get("name") or "").strip()
                    ok, err = channel_test(n)
                    log.info("渠道 %s 测试推送 %s（%s）", n, "成功" if ok else "失败",
                             self.client_address[0])
                    return self._json(200, {"ok": True, "success": ok, "error": err})
                if action == "channel/remove":
                    n = str(data.get("name") or "").strip()
                    set_channels([c for c in get_channels() if c["name"] != n])
                    log.info("渠道 %s 删除（%s）", n, self.client_address[0])
                    return self._json(200, {"ok": True})
                if action == "source/save":
                    item = data.get("item") or {}
                    cfg = item.setdefault("config", {})
                    typ = str(item.get("type") or "")
                    # Token/Secret 一律由服务端管理：为空（新建或留空）时自动生成；
                    # 编辑已有实例且未携带原值时保留原值，避免无关保存让旧值静默失效。
                    if typ in SOURCE_TYPES:
                        key = "secret" if typ == "github" else "token"
                        if not str(cfg.get(key) or "").strip():
                            keep = ""
                            old = str(data.get("original_name") or "").strip()
                            if old:
                                prev = next((s for s in get_sources()
                                             if s["name"] == old and s.get("type") == typ), None)
                                if prev:
                                    keep = str(((prev.get("config") or {}).get(key) or "")).strip()
                            cfg[key] = keep or secrets.token_urlsafe(24)
                    return _save_item(get_sources, set_sources, source_error, "通知源", "source")
                if action == "source/regen":
                    n = str(data.get("name") or "").strip()
                    items = get_sources()
                    idx = next((i for i, s in enumerate(items)
                                if s["name"] == n and s["type"] in SOURCE_TYPES), None)
                    if idx is None:
                        return self._json(404, {"ok": False, "error": "通知源不存在"})
                    key = "secret" if items[idx]["type"] == "github" else "token"
                    items[idx].setdefault("config", {})[key] = secrets.token_urlsafe(24)
                    set_sources(items)
                    log.info("通知源 %s %s 重新生成（%s）", n,
                             "Secret" if key == "secret" else "Token", self.client_address[0])
                    return self._json(200, {"ok": True, "item": items[idx]})
                if action == "source/remove":
                    n = str(data.get("name") or "").strip()
                    set_sources([s for s in get_sources() if s["name"] != n])
                    log.info("通知源 %s 删除（%s）", n, self.client_address[0])
                    return self._json(200, {"ok": True})
                if action == "logs/clear":
                    n = clear_logs()
                    log.info("清理推送历史 %s 条（%s）", n, self.client_address[0])
                    return self._json(200, {"ok": True, "deleted": n})

            if prefix == "retention" and name == "days":
                try:
                    v = int(data.get("value", LOG_RETENTION_DAYS))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "invalid retention days"})
                v = max(1, min(v, 3650))
                set_setting(key, str(v))
                with db() as conn:
                    conn.execute("DELETE FROM push_log WHERE ts < ?",
                                 (int(time.time()) - v * 86400,))
                log.info("设置 %s=%s（%s）", key, v, self.client_address[0])
                return self._json(200, {"ok": True, "key": key, "value": str(v)})
            valid = (prefix == "event" and name in EVENT_TYPES) or \
                    (prefix == "source" and any(s["name"] == name for s in get_sources())) or \
                    (prefix == "channel" and any(c["name"] == name for c in get_channels())) or \
                    (prefix == "notify" and name == "self")
            if not valid:
                return self._json(400, {"ok": False, "error": f"invalid key: {key}"})
            value = "1" if str(data.get("value", "0")) in ("1", "true", "True", "on") else "0"
            set_setting(key, value)
            log.info("设置 %s=%s（%s）", key, value, self.client_address[0])
            return self._json(200, {"ok": True, "key": key, "value": value})

        if path == "/login":
            if not (UI_AUTH_USER and UI_AUTH_PASS):
                return self._json(503, {"ok": False, "error": (
                    "面板未配置登录凭据（NOTIFY_AUTH_USER / NOTIFY_AUTH_PASS），已禁用面板访问")})
            if not self._same_origin():
                return self._json(403, {"ok": False, "error": "cross-origin request rejected"})
            try:
                raw = self._read_body().decode("utf-8", errors="replace")
            except BodyError as e:
                return self._json(e.status, {"ok": False, "error": e.error})
            data = urllib.parse.parse_qs(raw)
            user = (data.get("username") or [""])[0].strip()
            pwd = (data.get("password") or [""])[0].strip()
            if (hmac.compare_digest(user, UI_AUTH_USER)
                    and hmac.compare_digest(pwd, UI_AUTH_PASS)):
                token = ui_token(user)
                self.send_response(302)
                self.send_header("Set-Cookie",
                                 f"bb_token={token}; Path=/; HttpOnly; SameSite=Lax; "
                                 f"Max-Age={UI_SESSION_HOURS * 3600}"
                                 + ("; Secure" if self._is_https() else ""))
                self.send_header("Location", _abs("ui"))
                self.send_header("Content-Length", "0")
                self.end_headers()
                log.info("面板登录成功（%s）", self.client_address[0])
                return
            log.warning("面板登录失败 user=%r from %s", user, self.client_address[0])
            return self._redirect(_abs("login?error=1"))

        if path == "/logout":
            self.send_response(302)
            self.send_header("Set-Cookie", "bb_token=; Path=/; HttpOnly; Max-Age=0")
            self.send_header("Location", _abs("login"))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path.startswith("/hooks/"):
            key = path[len("/hooks/"):]
            entry = find_source(key)
            if not entry or entry["type"] not in SOURCES:
                return self._json(404, {"ok": False, "error": f"unknown source: {key}"})
            # 日志 / 审计 / 渠道分组一律用实例名，ID 只作稳定入口
            source = entry["name"]
            verify_fn = SOURCES[entry["type"]]["verify"]
            handle_fn = SOURCES[entry["type"]]["handle"]
            try:
                try:
                    body = self._read_body()
                except BodyError as e:
                    return self._json(e.status, {"ok": False, "error": e.error})
                if not verify_fn(self.headers, body, entry.get("config") or {}):
                    log.warning("%s 签名/token 校验失败 from %s", source, self.client_address[0])
                    return self._json(401, {"ok": False, "error": "bad signature"})
                delivery = self.headers.get("X-GitHub-Delivery") or f"{source}-{int(time.time() * 1000)}"
                if seen(delivery):
                    return self._json(200, {"ok": True, "dup": True})
                event = self.headers.get("X-GitHub-Event", "")
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except ValueError:
                    payload = body.decode("utf-8", errors="replace") if entry["type"] == "generic" else {}
                result = handle_fn(payload, event, entry.get("config") or {})
                if result is None:
                    return self._json(200, {"ok": True, "source": source, "ignored": True})
                title, msg, meta = result
                meta["source"] = source
                pushed = notify(title, msg, meta)
                return self._json(200, {"ok": True, "source": source, "pushed": pushed})
            except Exception:
                log.exception("处理 %s webhook 异常", source)
                return self._json(500, {"ok": False, "error": "internal error"})

        return self._json(404, {"ok": False, "error": "not found"})


def main():
    if not SECRET:
        log.warning("WEBHOOK_SECRET 未配置：github 来源需在面板生成 Secret，"
                    "未配 secret 的来源一律拒绝请求（401）")
    if not (UI_AUTH_USER and UI_AUTH_PASS):
        log.warning("NOTIFY_AUTH_USER / NOTIFY_AUTH_PASS 未配置：面板与 API 一律拒绝访问（fail-closed）")
    channels = ",".join(c["name"] for c in get_channels()) or "未配置"
    sources = ",".join(s["name"] for s in get_sources()) or "无"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("青鸟 Bluebird 监听 %s:%s（sources=%s, channels=%s, ui_auth=%s）",
             HOST, PORT, sources, channels, "on" if (UI_AUTH_USER and UI_AUTH_PASS) else "off")
    server.serve_forever()


if __name__ == "__main__":
    main()
