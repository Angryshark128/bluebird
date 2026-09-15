#!/usr/bin/env python3
"""青鸟 Bluebird 单元测试：签名校验 / 去重 / 来源解析 / 事件过滤 / 渠道分发 / 审计 / 开关 / 子路径。"""
import base64
import datetime
import hashlib
import hmac
import io
import json
import os
import sys
import unittest
import urllib.parse
from unittest import mock

TEST_DB = "/tmp/bluebird-test.db"

os.environ.update({
    "NOTIFY_OWNER": "testowner",
    "WEBHOOK_SECRET": "test-secret",
    "BARK_KEY": "test-key",
    "FEISHU_WEBHOOK": "https://mock.feishu",
    "FEISHU_SECRET": "s",
    "WECOM_WEBHOOK": "https://mock.wecom",
    "GENERIC_TOKEN": "",
    # 面板凭据：auth_ok 已 fail-closed，不配置凭据时面板/API 一律 503
    "NOTIFY_AUTH_USER": "admin",
    "NOTIFY_AUTH_PASS": "secret",
    "NOTIFY_DB": TEST_DB,
    "DEDUP_SECONDS": "60",
    "LOG_RETENTION_DAYS": "30",
    "BASE_PATH": "",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


def repo_payload(name="demo", owner="testowner"):
    return {"repository": {"name": name, "owner": {"login": owner},
                           "full_name": f"{owner}/{name}"}}


def test_channels():
    """三类渠道各一个实例（动态配置测试用）。"""
    return [
        {"name": "bark", "type": "bark", "config": {"key": "k"}},
        {"name": "feishu", "type": "feishu", "config": {"webhook": "https://f"}},
        {"name": "wecom", "type": "wecom", "config": {"webhook": "https://w"}},
    ]


# 面板认证已 fail-closed：API / 文档类测试统一带 Basic 凭据
PANEL_USER, PANEL_PWD = "admin", "secret"
AUTH_HEADER = {"Authorization": "Basic "
               + base64.b64encode(f"{PANEL_USER}:{PANEL_PWD}".encode()).decode()}


class VerifyTest(unittest.TestCase):
    def test_valid_signature(self):
        body = b'{"a":1}'
        sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(server.verify(sig, body))

    def test_invalid_signature(self):
        self.assertFalse(server.verify("sha256=" + "0" * 64, b"{}"))

    def test_wrong_prefix(self):
        self.assertFalse(server.verify("md5=abc", b"{}"))


class SeenTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_duplicate_rejected(self):
        self.assertFalse(server.seen("delivery-1"))
        self.assertTrue(server.seen("delivery-1"))
        self.assertFalse(server.seen("delivery-2"))


class HandleEventTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def _star_payload(self):
        repo = {**repo_payload()["repository"], "stargazers_count": 3}
        return {**repo_payload(), "repository": repo, "action": "started",
                "sender": {"login": "follower"}}

    def test_star(self):
        result = server._handle_github(self._star_payload(), "watch")
        self.assertIsNotNone(result)
        self.assertIn("star", result[0])
        self.assertEqual(result[2], {"event": "watch", "repo": "demo"})

    def test_source_scoped_events(self):
        """来源配置 events 白名单后，只处理列表内事件；未列事件忽略。"""
        cfg = {"events": ["watch"]}
        self.assertIsNotNone(server._handle_github(self._star_payload(), "watch", cfg))
        payload = {**repo_payload(), "action": "opened",
                   "sender": {"login": "someone"},
                   "issue": {"number": 1, "title": "x"}}
        self.assertIsNone(server._handle_github(payload, "issues", cfg))

    def test_empty_events_blocks_all(self):
        """来源显式配置 events=[] 后，任何事件都不推送（全不选 = 不推送）。"""
        cfg = {"events": []}
        self.assertIsNone(server._handle_github(self._star_payload(), "watch", cfg))
        self.assertIsNone(server._handle_github(
            {**repo_payload(), "action": "opened", "sender": {"login": "someone"},
             "issue": {"number": 1, "title": "x"}}, "issues", cfg))
        self.assertIsNone(server._handle_generic(
            {"title": "t", "body": "b", "event": "anything"}, cfg=cfg))

    def test_generic_scoped_events(self):
        """通用来源配置 events 后按 payload 事件字段过滤。"""
        cfg = {"events": ["server-monitor"]}
        self.assertIsNotNone(server._handle_generic(
            {"title": "t", "body": "b", "event": "server-monitor"}, cfg=cfg))
        self.assertIsNone(server._handle_generic(
            {"title": "t", "body": "b", "event": "other"}, cfg=cfg))

    def test_source_scoped_self(self):
        """来源级 self=true 时本人操作不再忽略（默认忽略）。"""
        payload = {**repo_payload(), "action": "started", "sender": {"login": "testowner"}}
        self.assertIsNone(server._handle_github(payload, "watch"))
        self.assertIsNotNone(server._handle_github(payload, "watch", {"self": True}))

    def test_star_self_ignored(self):
        payload = {**repo_payload(), "action": "started", "sender": {"login": "testowner"}}
        self.assertIsNone(server._handle_github(payload, "watch"))

    def test_star_bot_ignored(self):
        payload = {**repo_payload(), "action": "started", "sender": {"login": "dependabot[bot]"}}
        self.assertIsNone(server._handle_github(payload, "watch"))

    def test_star_self_notify_when_enabled(self):
        server.set_setting("notify.self", "1")
        payload = {**repo_payload(), "action": "started", "sender": {"login": "testowner"}}
        self.assertIsNotNone(server._handle_github(payload, "watch"))

    def test_star_bot_ignored_even_when_self_enabled(self):
        server.set_setting("notify.self", "1")
        payload = {**repo_payload(), "action": "started", "sender": {"login": "dependabot[bot]"}}
        self.assertIsNone(server._handle_github(payload, "watch"))

    def test_fork(self):
        payload = {**repo_payload(), "sender": {"login": "someone"}}
        self.assertIsNotNone(server._handle_github(payload, "fork"))

    def test_issue_opened(self):
        payload = {**repo_payload(), "action": "opened", "sender": {"login": "someone"},
                   "issue": {"number": 3, "title": "bug"}}
        result = server._handle_github(payload, "issues")
        self.assertIsNotNone(result)
        self.assertIn("#3", result[0])

    def test_issue_closed_ignored(self):
        payload = {**repo_payload(), "action": "closed", "sender": {"login": "someone"},
                   "issue": {"number": 3, "title": "bug"}}
        self.assertIsNone(server._handle_github(payload, "issues"))

    def test_issue_comment(self):
        payload = {**repo_payload(), "action": "created", "sender": {"login": "someone"},
                   "issue": {"number": 5}, "comment": {"body": "hello"}}
        result = server._handle_github(payload, "issue_comment")
        self.assertIsNotNone(result)
        self.assertIn("#5", result[0])

    def test_pull_request_opened(self):
        payload = {**repo_payload(), "action": "opened", "sender": {"login": "someone"},
                   "pull_request": {"number": 7, "title": "feat"}}
        self.assertIsNotNone(server._handle_github(payload, "pull_request"))

    def test_workflow_run_success(self):
        payload = {**repo_payload(), "action": "completed",
                   "workflow": {"name": "ci"},
                   "workflow_run": {"name": "ci", "status": "completed", "conclusion": "success",
                                    "head_branch": "main", "display_title": "fix"}}
        result = server._handle_github(payload, "workflow_run")
        self.assertIsNotNone(result)
        self.assertIn("通过", result[0])

    def test_workflow_run_skipped_ignored(self):
        payload = {**repo_payload(), "action": "completed",
                   "workflow_run": {"status": "completed", "conclusion": "skipped"}}
        self.assertIsNone(server._handle_github(payload, "workflow_run"))

    def test_workflow_run_in_progress_ignored(self):
        payload = {**repo_payload(), "action": "in_progress",
                   "workflow_run": {"status": "in_progress"}}
        self.assertIsNone(server._handle_github(payload, "workflow_run"))

    def test_other_owner_ignored(self):
        payload = {**repo_payload(owner="someone"), "action": "started",
                   "sender": {"login": "follower"}}
        self.assertIsNone(server._handle_github(payload, "watch"))

    def test_owner_empty_processes_all_repos(self):
        """NOTIFY_OWNER 留空 = 处理所有仓库的事件（不再按 owner 过滤）。"""
        prev = server.NOTIFY_OWNER
        server.NOTIFY_OWNER = ""
        try:
            payload = {**repo_payload(owner="someone"), "action": "started",
                       "sender": {"login": "follower"}}
            self.assertIsNotNone(server._handle_github(payload, "watch"))
        finally:
            server.NOTIFY_OWNER = prev

    def test_unknown_event(self):
        self.assertIsNone(server._handle_github(repo_payload(), "release"))


class NotifyTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self._prev = server.get_channels()
        server.set_channels(test_channels())

    def tearDown(self):
        server.set_channels(self._prev)

    @mock.patch("server._push_channel", return_value=True)
    def test_all_channels_called(self, push):
        self.assertTrue(server.notify("t", "b", meta={"event": "watch", "repo": "demo"}))
        self.assertEqual(push.call_count, 3)
        self.assertEqual({c.args[0] for c in push.call_args_list}, {"bark", "feishu", "wecom"})
        self.assertTrue(all(c.args[1:3] == ("t", "b") for c in push.call_args_list))

    @mock.patch("server._push_channel", side_effect=RuntimeError("boom"))
    def test_one_channel_error_does_not_block(self, push):
        # 第一个渠道抛异常不应阻断后续渠道推送，但全部失败时 notify 返回 False
        with mock.patch("server._push_channel",
                        side_effect=lambda t, title, body, cfg, source="":
                        (_ for _ in ()).throw(RuntimeError("boom")) if t == "feishu" else True):
            self.assertTrue(server.notify("t", "b"))

    @mock.patch("server._push_channel", return_value=False)
    def test_all_failed(self, push):
        self.assertFalse(server.notify("t", "b"))

    @mock.patch("server._push_channel", return_value=True)
    def test_source_scoped_channels(self, push):
        """来源配置 channels 后，只推送到指定渠道。"""
        self._prev_src = server.get_sources()
        server.set_sources([{"name": "generic", "type": "generic",
                             "config": {"token": "t", "channels": ["bark", "wecom"]}}])
        try:
            self.assertTrue(server.notify("t", "b", meta={"source": "generic"}))
            self.assertEqual({c.args[0] for c in push.call_args_list}, {"bark", "wecom"})
        finally:
            server.set_sources(self._prev_src)

    @mock.patch("server._push_channel", return_value=True)
    def test_empty_channels_blocks_all(self, push):
        """来源显式配置 channels=[] 后不推送任何渠道（全不选 = 不推送）。"""
        self._prev_src = server.get_sources()
        server.set_sources([{"name": "generic", "type": "generic",
                             "config": {"token": "t", "channels": []}}])
        try:
            self.assertFalse(server.notify("t", "b", meta={"source": "generic"}))
            self.assertEqual(push.call_count, 0)
        finally:
            server.set_sources(self._prev_src)

    @mock.patch("server._push_channel", return_value=True)
    def test_unknown_source_uses_all_channels(self, push):
        """meta 未携带来源或来源不存在时，推送到全部已启用渠道。"""
        self.assertTrue(server.notify("t", "b", meta={"source": "no-such-source"}))
        self.assertEqual({c.args[0] for c in push.call_args_list}, {"bark", "feishu", "wecom"})


class PushChannelTest(unittest.TestCase):
    """PushDeer / 通用 Webhook 渠道的请求构造与成功判定。"""

    @mock.patch("server._post_form")
    def test_pushdeer_success(self, post):
        post.return_value = {"code": 0, "content": {"result": ['{"success":"ok"}']}}
        ok = server._push_channel("pushdeer", "标题", "正文",
                                  {"key": "k1,k2", "url": "https://pd.example.com"})
        self.assertTrue(ok)
        url, payload = post.call_args[0]
        self.assertEqual(url, "https://pd.example.com/message/push")
        self.assertEqual(payload, {"pushkey": "k1,k2", "text": "标题",
                                   "desp": "正文", "type": "markdown"})

    @mock.patch("server._post_form")
    def test_pushdeer_default_url(self, post):
        post.return_value = {"code": 0}
        server._push_channel("pushdeer", "t", "b", {"key": "k"})
        self.assertEqual(post.call_args[0][0], "https://api2.pushdeer.com/message/push")

    @mock.patch("server._post_form")
    def test_pushdeer_failure_code(self, post):
        """PushDeer 失败时 HTTP 仍是 200，只能靠 code 判断。"""
        post.return_value = {"code": 80501, "error": "错误的Key"}
        self.assertFalse(server._push_channel("pushdeer", "t", "b", {"key": "k"}))

    def test_pushdeer_missing_key(self):
        self.assertFalse(server._push_channel("pushdeer", "t", "b", {}))

    @mock.patch("server._post_raw")
    def test_webhook_payload(self, post):
        post.return_value = True
        ok = server._push_channel("webhook", "标题", "正文",
                                  {"url": "https://hook.example.com/x"}, source="github")
        self.assertTrue(ok)
        url, body, headers = post.call_args[0]
        self.assertEqual(url, "https://hook.example.com/x")
        self.assertEqual(json.loads(body.decode("utf-8")),
                         {"title": "标题", "body": "正文", "source": "github"})
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("X-Bluebird-Signature-256", headers)

    @mock.patch("server._post_raw")
    def test_webhook_signature(self, post):
        post.return_value = True
        server._push_channel("webhook", "t", "b", {"url": "https://h/x", "secret": "s3cret"})
        body, headers = post.call_args[0][1], post.call_args[0][2]
        expect = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Bluebird-Signature-256"], expect)

    @mock.patch("server._post_raw")
    def test_webhook_non_2xx(self, post):
        post.return_value = False
        self.assertFalse(server._push_channel("webhook", "t", "b", {"url": "https://h/x"}))

    def test_webhook_missing_url(self):
        self.assertFalse(server._push_channel("webhook", "t", "b", {}))


class ChannelValidateTest(unittest.TestCase):
    def test_valid(self):
        self.assertIsNone(server.channel_error(
            {"name": "pd", "type": "pushdeer", "config": {"key": "k"}}))
        self.assertIsNone(server.channel_error(
            {"name": "wh", "type": "webhook", "config": {"url": "https://h/x"}}))

    def test_missing_required(self):
        self.assertIn("Key", server.channel_error({"name": "pd", "type": "pushdeer", "config": {}}))
        self.assertIn("URL", server.channel_error({"name": "wh", "type": "webhook", "config": {}}))

    def test_unknown_type(self):
        self.assertIn("未知渠道类型", server.channel_error(
            {"name": "x", "type": "nope", "config": {}}))


class DefaultChannelEnvTest(unittest.TestCase):
    """首次启动时从环境变量迁移出的初始渠道实例。"""

    def test_pushdeer_and_webhook_from_env(self):
        with mock.patch.multiple(server, PUSHDEER_KEY="pk", PUSHDEER_URL="https://pd.example.com",
                                 GENERIC_WEBHOOK_URL="https://hook.example.com/x",
                                 GENERIC_WEBHOOK_SECRET="s"):
            lst = server._default_channels_from_env()
        by_name = {c["name"]: c for c in lst}
        self.assertEqual(by_name["pushdeer"]["type"], "pushdeer")
        self.assertEqual(by_name["pushdeer"]["config"],
                         {"url": "https://pd.example.com", "key": "pk"})
        self.assertEqual(by_name["webhook"]["config"],
                         {"url": "https://hook.example.com/x", "secret": "s"})

    def test_absent_env_adds_nothing(self):
        with mock.patch.multiple(server, PUSHDEER_KEY="", GENERIC_WEBHOOK_URL=""):
            names = [c["name"] for c in server._default_channels_from_env()]
        self.assertNotIn("pushdeer", names)
        self.assertNotIn("webhook", names)


class ChannelTestTest(unittest.TestCase):
    """面板「测试推送」：只打目标实例、不计入推送记录。"""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self._prev = server.get_channels()
        server.set_channels(test_channels())

    def tearDown(self):
        server.set_channels(self._prev)

    @mock.patch("server._push_channel", return_value=True)
    def test_success(self, push):
        ok, err = server.channel_test("bark")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        typ, title, body, cfg, source = push.call_args[0]
        self.assertEqual(typ, "bark")
        self.assertIn("测试推送", title)
        self.assertIn("bark", body)
        self.assertEqual(cfg, {"key": "k"})
        self.assertEqual(source, "test")

    @mock.patch("server._push_channel", return_value=False)
    def test_failure_reported(self, push):
        ok, err = server.channel_test("feishu")
        self.assertFalse(ok)
        self.assertIn("失败", err)

    @mock.patch("server._push_channel", side_effect=RuntimeError("boom"))
    def test_exception_reported(self, push):
        ok, err = server.channel_test("wecom")
        self.assertFalse(ok)
        self.assertIn("boom", err)

    def test_unknown_channel(self):
        ok, err = server.channel_test("no-such-channel")
        self.assertFalse(ok)
        self.assertIn("不存在", err)

    @mock.patch("server._push_channel", return_value=True)
    def test_not_logged(self, push):
        """测试推送不写审计日志，避免污染推送记录与统计。"""
        server.channel_test("bark")
        self.assertEqual(server.count_logs(days=1)["total"], 0)


class SourceIdTest(unittest.TestCase):
    """来源稳定 ID：/hooks/<ID> 不随改名失效，名字形式仍兼容。"""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self._prev = server.get_sources()

    def tearDown(self):
        server.set_sources(self._prev)

    def test_ids_assigned_lazily_and_stable(self):
        """旧配置（无 id）读取时补 ID 并落库；再读 ID 不变。"""
        server.set_setting("sources.list", json.dumps([
            {"name": "a", "type": "generic", "config": {}},
            {"name": "b", "type": "generic", "config": {}}]))
        ids = [s.get("id") for s in server.get_sources()]
        self.assertTrue(all(ids))
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual([s["id"] for s in server.get_sources()], ids)

    def test_find_source_by_id_and_name(self):
        server.set_sources([{"id": "abc123", "name": "home-mac", "type": "generic",
                             "config": {"token": "t"}}])
        self.assertEqual(server.find_source("abc123")["name"], "home-mac")
        self.assertEqual(server.find_source("home-mac")["id"], "abc123")
        self.assertIsNone(server.find_source("nope"))

    def test_renamed_source_keeps_id(self):
        """改名后按 ID 仍能定位（名字形式则随之失效）。"""
        server.set_sources([{"id": "fixed-id", "name": "new-name", "type": "generic",
                             "config": {"token": "t"}}])
        self.assertEqual(server.find_source("fixed-id")["name"], "new-name")
        self.assertIsNone(server.find_source("old-name"))


class AuditLogTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self._prev = server.get_channels()
        server.set_channels(test_channels())
        # 清除 BASE_PATH 让 HTTP 层测试走根路径；SubPathTest 另行覆盖子路径模式
        self._prev_base = server.BASE_PATH
        server.BASE_PATH = ""

    def tearDown(self):
        server.set_channels(self._prev)
        server.BASE_PATH = self._prev_base

    @mock.patch("server._push_channel", return_value=True)
    def test_notify_records_log(self, push):
        self.assertTrue(server.notify("⭐ a star 了 demo", "body",
                                      meta={"event": "watch", "repo": "demo", "source": "github"}))
        rows = server.query_logs()
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["channel"] for r in rows}, {"bark", "feishu", "wecom"})
        self.assertTrue(all(r["status"] == "ok" for r in rows))
        self.assertTrue(all(r["event_type"] == "watch" and r["repo"] == "demo"
                            and r["source"] == "github" for r in rows))

    def test_query_logs_filter(self):
        now = int(__import__("time").time())
        server.log_push("bark", "watch", "demo", "t1", "b1", "ok", ts=now - 200, source="github")
        server.log_push("bark", "fork", "demo", "t2", "b2", "error", ts=now - 100, source="github")
        server.log_push("wecom", "generic", "", "t3", "b3", "ok", ts=now, source="generic")
        self.assertEqual(len(server.query_logs()), 3)
        self.assertEqual([r["title"] for r in server.query_logs(channel="bark")], ["t2", "t1"])
        self.assertEqual([r["title"] for r in server.query_logs(event_type="watch")], ["t1"])
        self.assertEqual([r["title"] for r in server.query_logs(source="generic")], ["t3"])
        self.assertEqual([r["title"] for r in server.query_logs(channel="bark", event_type="watch")],
                         ["t1"])

    def test_query_logs_pagination(self):
        """offset 翻页 + count_logs 总数/成功失败统计。"""
        now = int(__import__("time").time())
        for i in range(5):
            server.log_push("bark", "watch", "demo", f"t{i}", "b",
                            "ok" if i % 2 == 0 else "error",
                            ts=now - (5 - i), source="github")
        self.assertEqual(server.count_logs(), {"total": 5, "ok": 3, "error": 2})
        self.assertEqual(server.count_logs(channel="bark", event_type="watch"),
                         {"total": 5, "ok": 3, "error": 2})
        self.assertEqual(server.count_logs(event_type="fork"), {"total": 0, "ok": 0, "error": 0})
        self.assertEqual([r["title"] for r in server.query_logs(limit=2, offset=0)], ["t4", "t3"])
        self.assertEqual([r["title"] for r in server.query_logs(limit=2, offset=2)], ["t2", "t1"])
        self.assertEqual([r["title"] for r in server.query_logs(limit=2, offset=4)], ["t0"])
        self.assertEqual(server.query_logs(limit=2, offset=10), [])

    def _get_logs(self, query=""):
        h = server.Handler.__new__(server.Handler)
        h.path = "/api/logs" + ("?" + query if query else "")
        h.headers = dict(AUTH_HEADER)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses = []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = mock.Mock()
        h.end_headers = mock.Mock()
        h.do_GET()
        return statuses, json.loads(h.wfile.getvalue().decode())

    def test_logs_api_pagination(self):
        """HTTP 层：/api/logs 支持 page/page_size 翻页，并返回全量统计。"""
        now = int(__import__("time").time())
        for i in range(5):
            server.log_push("bark", "watch", "demo", f"t{i}", "b",
                            "ok" if i % 2 == 0 else "error",
                            ts=now - (5 - i), source="github")
        statuses, d = self._get_logs("page=2&page_size=2")
        self.assertIn(200, statuses)
        self.assertEqual(d["total"], 5)
        self.assertEqual(d["ok_count"], 3)
        self.assertEqual(d["error_count"], 2)
        self.assertEqual(d["page"], 2)
        self.assertEqual(d["page_size"], 2)
        self.assertEqual([it["title"] for it in d["items"]], ["t2", "t1"])

    def test_query_stats_status(self):
        """按结果维度聚合：成功/失败各计，日期轴补全，窗口外数据不计。"""
        now = int(__import__("time").time())
        server.log_push("bark", "watch", "demo", "t1", "b1", "ok", ts=now - 200, source="github")
        server.log_push("bark", "fork", "demo", "t2", "b2", "error", ts=now - 100, source="github")
        server.log_push("wecom", "generic", "", "t3", "b3", "ok", ts=now, source="generic")
        server.log_push("bark", "watch", "demo", "old", "b", "ok",
                        ts=now - 8 * 86400, source="github")   # 8 天前，超出 7 天窗口
        s = server.query_stats(days=7, group="status")
        self.assertEqual({x["key"] for x in s["series"]}, {"ok", "error"})
        today = datetime.date.today().strftime("%Y-%m-%d")
        self.assertIn(today, s["labels"])
        self.assertEqual(len(s["labels"]), 7)
        by = {x["key"]: dict(zip(s["labels"], x["values"])) for x in s["series"]}
        self.assertEqual(by["ok"][today], 2)
        self.assertEqual(by["error"][today], 1)
        self.assertEqual(sum(by["ok"].values()) + sum(by["error"].values()), 3)

    def test_query_stats_source(self):
        """按通知源维度聚合；空 source 归入 '(空)'。"""
        now = int(__import__("time").time())
        server.log_push("bark", "watch", "demo", "t1", "b1", "ok", ts=now, source="github")
        server.log_push("wecom", "generic", "", "t3", "b3", "ok", ts=now, source="generic")
        server.log_push("bark", "fork", "demo", "t2", "b2", "ok", ts=now, source="")
        s = server.query_stats(days=7, group="source")
        keys = {x["key"] for x in s["series"]}
        self.assertEqual(keys, {"github", "generic", "(空)"})

    def test_cleanup_expired(self):
        now = int(__import__("time").time())
        server.log_push("bark", "watch", "demo", "old", "b", "ok", ts=now - 31 * 86400)
        server.log_push("bark", "fork", "demo", "new", "b", "ok", ts=now)
        rows = server.query_logs(days=30)
        self.assertEqual([r["title"] for r in rows], ["new"])

    def test_clear_logs(self):
        now = int(__import__("time").time())
        server.log_push("bark", "watch", "demo", "t1", "b1", "ok", ts=now - 200)
        server.log_push("wecom", "generic", "", "t2", "b2", "ok", ts=now)
        self.assertEqual(len(server.query_logs()), 2)
        self.assertEqual(server.clear_logs(), 2)
        self.assertEqual(server.query_logs(), [])
        self.assertEqual(server.clear_logs(), 0)


class SinceFilterTest(unittest.TestCase):
    """since（epoch 秒下限）过滤：query_logs / count_logs 函数层 + HTTP /api/logs?since=…。"""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        now = int(__import__("time").time())
        self._now = now
        # t2（半小时前）、t1（1 小时前）、t0（2 天前）
        server.log_push("bark", "watch", "demo", "t2", "b2", "ok", ts=now - 1800)
        server.log_push("bark", "fork", "demo", "t1", "b1", "error", ts=now - 3600)
        server.log_push("wecom", "generic", "", "t0", "b0", "ok", ts=now - 2 * 86400)

    def test_query_logs_since(self):
        """since 只保留 ts>=since 的记录；不传 since 时行为不变。"""
        since = self._now - 3600
        self.assertEqual([r["title"] for r in server.query_logs(since=since)], ["t2", "t1"])
        self.assertEqual([r["title"] for r in server.query_logs(since=self._now - 2700)], ["t2"])
        self.assertEqual(server.query_logs(since=self._now + 60), [])        # 未来下限 → 空
        self.assertEqual(len(server.query_logs()), 3)                        # 无 since → 原样

    def test_query_logs_since_stacks_with_days_and_channel(self):
        """since 与 days / channel 条件叠加取交集。"""
        since = self._now - 3600
        self.assertEqual([r["title"] for r in server.query_logs(days=30, since=since)], ["t2", "t1"])
        self.assertEqual([r["title"] for r in server.query_logs(channel="bark", since=since)],
                         ["t2", "t1"])
        self.assertEqual(server.query_logs(channel="wecom", since=since), [])  # wecom 只有 2 天前那条

    def test_count_logs_since(self):
        """since 对统计口径生效（总数/成功/失败）。"""
        since = self._now - 3600
        self.assertEqual(server.count_logs(since=since), {"total": 2, "ok": 1, "error": 1})
        self.assertEqual(server.count_logs(days=30, since=since),
                         {"total": 2, "ok": 1, "error": 1})
        self.assertEqual(server.count_logs(since=self._now + 60), {"total": 0, "ok": 0, "error": 0})

    def _get_logs(self, query=""):
        h = server.Handler.__new__(server.Handler)
        h.path = "/api/logs" + ("?" + query if query else "")
        h.headers = dict(AUTH_HEADER)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses = []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = mock.Mock()
        h.end_headers = mock.Mock()
        h.do_GET()
        return statuses, json.loads(h.wfile.getvalue().decode())

    def test_logs_api_since(self):
        """HTTP 层：/api/logs?since=… 过滤生效且与分页统计一致。"""
        since = self._now - 3600
        statuses, d = self._get_logs("since=%d&page=1&page_size=10" % since)
        self.assertIn(200, statuses)
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["ok_count"], 1)
        self.assertEqual(d["error_count"], 1)
        self.assertEqual([it["title"] for it in d["items"]], ["t2", "t1"])

    def test_logs_api_invalid_since_ignored(self):
        """非法 since 视为未传：回退默认近 7 天，全部记录返回。"""
        statuses, d = self._get_logs("since=abc&page=1&page_size=10")
        self.assertIn(200, statuses)
        self.assertEqual(d["total"], 3)


class AuthTest(unittest.TestCase):
    def setUp(self):
        self._user, self._pwd = server.UI_AUTH_USER, server.UI_AUTH_PASS
        server.UI_AUTH_USER, server.UI_AUTH_PASS = "admin", "secret"

    def tearDown(self):
        server.UI_AUTH_USER, server.UI_AUTH_PASS = self._user, self._pwd

    def test_no_credentials_rejected(self):
        """面板凭据未配置齐全（缺用户名或缺密码）时一律拒绝访问：fail-closed。"""
        handler = mock.Mock(headers={})
        server.UI_AUTH_USER = ""
        self.assertFalse(server.auth_ok(handler))
        server.UI_AUTH_USER, server.UI_AUTH_PASS = "admin", ""
        self.assertFalse(server.auth_ok(handler))

    def test_valid_credentials(self):
        token = base64.b64encode(b"admin:secret").decode()
        handler = mock.Mock(headers={"Authorization": f"Basic {token}"})
        self.assertTrue(server.auth_ok(handler))

    def test_invalid_credentials(self):
        token = base64.b64encode(b"admin:wrong").decode()
        handler = mock.Mock(headers={"Authorization": f"Basic {token}"})
        self.assertFalse(server.auth_ok(handler))

    def test_missing_header(self):
        handler = mock.Mock(headers={})
        self.assertFalse(server.auth_ok(handler))

    def test_ui_token_roundtrip(self):
        token = server.ui_token("admin")
        self.assertTrue(server.ui_token_ok(token))

    def test_ui_token_tampered(self):
        token = server.ui_token("admin")
        self.assertFalse(server.ui_token_ok(token[:-2] + "00"))

    def test_ui_token_wrong_user(self):
        token = server.ui_token("admin").replace("admin", "hacker", 1)
        self.assertFalse(server.ui_token_ok(token))

    def test_ui_token_expired(self):
        user, _, _ = server.ui_token("admin").rsplit(".", 2)
        payload = f"{user}.1"
        sig = hmac.new(server.session_secret().encode(), payload.encode(),
                       hashlib.sha256).hexdigest()
        self.assertFalse(server.ui_token_ok(f"{payload}.{sig}"))

    def test_cookie_auth(self):
        token = server.ui_token("admin")
        handler = mock.Mock(headers={"Cookie": f"bb_token={token}"})
        self.assertTrue(server.auth_ok(handler))

    def test_bad_cookie_falls_back_to_basic(self):
        handler = mock.Mock(headers={
            "Cookie": "bb_token=bad",
            "Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()})
        self.assertTrue(server.auth_ok(handler))


class SessionSecretTest(unittest.TestCase):
    """面板会话密钥：不得回退到可预测常量，否则任何人都能伪造登录态。"""

    def test_unpredictable_and_stable(self):
        """session_secret() 非可预测常量，且同进程内多次调用一致（不随机漂移）。"""
        secret = server.session_secret()
        self.assertNotEqual(secret, "bluebird")
        self.assertTrue(secret)
        self.assertEqual(server.session_secret(), secret)


class ToggleTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_default_enabled(self):
        self.assertTrue(server.is_enabled("event", "watch"))
        self.assertTrue(server.is_enabled("channel", "bark"))
        self.assertTrue(server.is_enabled("source", "github"))

    def test_set_and_read(self):
        server.set_setting("event.watch", "0")
        self.assertFalse(server.is_enabled("event", "watch"))
        server.set_setting("event.watch", "1")
        self.assertTrue(server.is_enabled("event", "watch"))

    def test_disabled_event_not_notified(self):
        server.set_setting("event.watch", "0")
        payload = {**repo_payload(), "action": "started", "sender": {"login": "follower"}}
        self.assertIsNone(server._handle_github(payload, "watch"))

    def test_disabled_source_blocks(self):
        server.set_setting("source.generic", "0")
        self.assertIsNotNone(server._handle_generic({"title": "t", "body": "b"}))
        self.assertFalse(server.is_enabled("source", "generic"))

    def test_disabled_channel_skipped(self):
        server.set_setting("channel.bark", "0")
        with mock.patch("server._push_channel", return_value=True) as push:
            server.notify("t", "b", meta={"event": "watch", "repo": "demo"})
        self.assertEqual(push.call_count, 2)
        self.assertNotIn("bark", [c.args[0] for c in push.call_args_list])

    def _post_settings(self, payload):
        """构造 Handler 调用 do_POST /api/settings，返回 send_response 的 status 列表。"""
        body = json.dumps(payload).encode()
        h = server.Handler.__new__(server.Handler)
        h.path = "/api/settings"
        h.headers = {**AUTH_HEADER, "Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses = []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = mock.Mock()
        h.end_headers = mock.Mock()
        h.do_POST()
        return statuses

    def test_instance_toggle_keys_accepted(self):
        """开关 key 支持实例名（source.<实例名> / channel.<实例名>），而非仅内置类型名。"""
        server.set_sources([{"name": "demo-app", "type": "generic",
                             "config": {"token": "t"}}])
        server.set_channels([{"name": "iphone", "type": "bark", "config": {"key": "k"}}])
        self.assertIn(200, self._post_settings({"key": "source.demo-app", "value": 0}))
        self.assertIn(200, self._post_settings({"key": "channel.iphone", "value": 0}))
        self.assertFalse(server.is_enabled("source", "demo-app"))
        self.assertFalse(server.is_enabled("channel", "iphone"))

    def test_invalid_toggle_key_rejected(self):
        """未知前缀/名称的开关 key 返回 400。"""
        self.assertIn(400, self._post_settings({"key": "source.no-such-instance", "value": 0}))
        self.assertIn(400, self._post_settings({"key": "bogus.anything", "value": 0}))

    def _get_settings(self):
        h = server.Handler.__new__(server.Handler)
        h.path = "/api/settings"
        h.headers = dict(AUTH_HEADER)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses = []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = mock.Mock()
        h.end_headers = mock.Mock()
        h.do_GET()
        return statuses, json.loads(h.wfile.getvalue().decode())

    def test_settings_returns_instance_toggles(self):
        """/api/settings 的 sources/channels 开关字典按实例名返回（而非内置类型名），
        保证前端 switch 状态能正确刷新。"""
        server.set_sources([{"name": "demo-app", "type": "generic",
                             "config": {"token": "t"}}])
        server.set_channels([{"name": "iphone", "type": "bark", "config": {"key": "k"}}])
        server.set_setting("source.demo-app", "0")
        server.set_setting("channel.iphone", "0")
        statuses, d = self._get_settings()
        self.assertIn(200, statuses)
        self.assertIn("demo-app", d["sources"])
        self.assertFalse(d["sources"]["demo-app"])
        self.assertIn("iphone", d["channels"])
        self.assertFalse(d["channels"]["iphone"])

    def _get_docs(self, path):
        h = server.Handler.__new__(server.Handler)
        h.path = path
        h.headers = dict(AUTH_HEADER)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses = []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = mock.Mock()
        h.end_headers = mock.Mock()
        h.do_GET()
        return statuses, h.wfile.getvalue()

    def test_docs_route_serves_markdown(self):
        statuses, body = self._get_docs("/docs/add-source-and-channel.md")
        self.assertIn(200, statuses)
        self.assertIn(b"## ", body)

    def test_docs_route_rejects_traversal(self):
        statuses, _ = self._get_docs("/docs/../server.py")
        self.assertIn(404, statuses)
        statuses, _ = self._get_docs("/docs/")
        self.assertIn(404, statuses)

    def test_notify_self_default_off(self):
        self.assertFalse(server.is_enabled("notify", "self", default="0"))

    def test_notify_self_toggle(self):
        server.set_setting("notify.self", "1")
        self.assertTrue(server.is_enabled("notify", "self", default="0"))
        server.set_setting("notify.self", "0")
        self.assertFalse(server.is_enabled("notify", "self", default="0"))

    def test_retention_default(self):
        self.assertEqual(server.retention_days(), server.LOG_RETENTION_DAYS)

    def test_retention_override_and_cleanup(self):
        server.set_setting("retention.days", "7")
        self.assertEqual(server.retention_days(), 7)
        now = int(__import__("time").time())
        server.log_push("bark", "watch", "demo", "old", "b", "ok", ts=now - 10 * 86400)
        server.log_push("bark", "watch", "demo", "new", "b", "ok", ts=now)
        self.assertEqual([r["title"] for r in server.query_logs(days=30)], ["new"])


class RenameHistoryTest(unittest.TestCase):
    """实例改名后历史日志与启停开关跟随新名称。"""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self._prev = server.get_channels()
        server.set_channels(test_channels())

    def tearDown(self):
        server.set_channels(self._prev)

    def test_rename_channel_updates_logs_and_toggle(self):
        server.log_push("bark", "watch", "demo", "t", "b", "ok",
                        ts=int(__import__("time").time()))
        server.set_setting("channel.bark", "0")
        server.rename_history("channel", "bark", "ios")
        self.assertEqual(server.query_logs()[0]["channel"], "ios")
        self.assertFalse(server.is_enabled("channel", "ios"))
        self.assertIsNone(server.get_settings().get("channel.bark"))

    def test_rename_channel_updates_source_refs(self):
        """渠道改名后，引用它的来源 channels 配置同步更新。"""
        self._prev_src = server.get_sources()
        server.set_sources([{"name": "s1", "type": "generic",
                             "config": {"token": "t", "channels": ["bark", "feishu"]}}])
        try:
            server.rename_history("channel", "bark", "ios")
            s = server.get_sources()[0]
            self.assertEqual(s["config"]["channels"], ["ios", "feishu"])
        finally:
            server.set_sources(self._prev_src)

    def test_rename_source_updates_logs_and_toggle(self):
        server.log_push("bark", "watch", "demo", "t", "b", "ok",
                        source="old-src", ts=int(__import__("time").time()))
        server.set_setting("source.old-src", "0")
        server.rename_history("source", "old-src", "new-src")
        self.assertEqual(server.query_logs()[0]["source"], "new-src")
        self.assertFalse(server.is_enabled("source", "new-src"))
        self.assertIsNone(server.get_settings().get("source.old-src"))


class NameValidationTest(unittest.TestCase):
    """实例名称字符约束（用于 Webhook 路径 /hooks/<名称>）。"""

    def _src(self, name):
        return {"name": name, "type": "generic", "config": {"token": "t"}}

    def test_source_legal_names(self):
        for name in ("github", "server-monitor", "my_source", "a.b-c_d", "abc123"):
            self.assertIsNone(server.source_error(self._src(name)), name)

    def test_source_illegal_names(self):
        for name in ("[Github] Angryshark128", "my source", "名字", "a/b", "a?b", "a@b"):
            self.assertIsNotNone(server.source_error(self._src(name)), name)

    def test_source_name_length(self):
        self.assertIsNotNone(server.source_error(self._src("x" * 33)))

    def test_channel_illegal_names(self):
        item = {"name": "my bark", "type": "bark", "config": {"key": "k"}}
        self.assertIsNotNone(server.channel_error(item))

    def test_retention_invalid_falls_back(self):
        server.set_setting("retention.days", "abc")
        self.assertEqual(server.retention_days(), server.LOG_RETENTION_DAYS)


class SourceRegistryTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self._token = server.GENERIC_TOKEN
        self._secret = server.SECRET
        server.SECRET = "test-secret"

    def tearDown(self):
        server.GENERIC_TOKEN = self._token
        server.SECRET = self._secret

    def test_registry_has_sources(self):
        self.assertIn("github", server.SOURCES)
        self.assertIn("generic", server.SOURCES)

    def test_verify_github(self):
        body = b'{"a":1}'
        cfg = {"secret": "test-secret"}
        sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(server._verify_github({"X-Hub-Signature-256": sig}, body, cfg))
        self.assertFalse(server._verify_github({"X-Hub-Signature-256": "sha256=000"}, body, cfg))
        # 未配 secret 一律拒绝（fail-closed），避免无认证注入
        self.assertFalse(server._verify_github({}, body, {"secret": ""}))

    def test_verify_generic(self):
        cfg = {"token": "tok"}
        # 标准 Authorization: Bearer
        self.assertTrue(server._verify_generic({"Authorization": "Bearer tok"}, b"{}", cfg))
        # 兼容裸 Authorization（无 scheme）
        self.assertTrue(server._verify_generic({"Authorization": "tok"}, b"{}", cfg))
        # 历史自定义头 X-Notify-Token 已移除：带这个头也不认（只认 Authorization）
        self.assertFalse(server._verify_generic({"X-Notify-Token": "tok"}, b"{}", cfg))
        # Basic 只用于面板认证，不参与来源 token 校验
        self.assertFalse(server._verify_generic({"Authorization": "Basic tok"}, b"{}", cfg))
        self.assertFalse(server._verify_generic({"Authorization": "Bearer bad"}, b"{}", cfg))
        self.assertFalse(server._verify_generic({}, b"{}", cfg))
        self.assertFalse(server._verify_generic({}, b"{}", {"token": ""}))

    def test_handle_generic_json(self):
        result = server._handle_generic({"title": "t", "body": "b", "event": "deploy", "repo": "r"})
        self.assertEqual(result, ("t", "b", {"event": "deploy", "repo": "r"}))

    def test_handle_generic_message_key(self):
        result = server._handle_generic({"message": "发布完成", "content": "v1.0 已上线"})
        self.assertEqual(result[0], "发布完成")
        self.assertEqual(result[2]["event"], "generic")

    def test_handle_generic_text(self):
        result = server._handle_generic("第一行标题\n第二行内容")
        self.assertEqual(result[0], "第一行标题")
        self.assertIn("第二行", result[1])

    def test_handle_generic_empty(self):
        self.assertIsNone(server._handle_generic({}))
        self.assertIsNone(server._handle_generic("  "))


class DynamicConfigTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_migrate_channels_from_env(self):
        channels = server.get_channels()
        self.assertEqual({c["name"] for c in channels}, {"bark", "feishu", "wecom"})
        bark = next(c for c in channels if c["name"] == "bark")
        self.assertEqual(bark["type"], "bark")
        self.assertEqual(bark["config"]["key"], "test-key")
        self.assertEqual(bark["config"]["url"], "https://api.day.app")

    def test_migrate_sources_from_env(self):
        sources = server.get_sources()
        self.assertEqual([s["name"] for s in sources], ["github"])
        self.assertEqual(sources[0]["type"], "github")
        self.assertEqual(sources[0]["config"]["secret"], "test-secret")

    def test_channel_validation(self):
        self.assertIsNone(server.channel_error(
            {"name": "b2", "type": "bark", "config": {"key": "k"}}))
        self.assertIsNotNone(server.channel_error(
            {"name": "b2", "type": "bark", "config": {}}))
        self.assertIsNotNone(server.channel_error(
            {"name": "", "type": "bark", "config": {"key": "k"}}))
        self.assertIsNotNone(server.channel_error(
            {"name": "x", "type": "slack", "config": {}}))

    def test_source_validation(self):
        self.assertIsNone(server.source_error(
            {"name": "g2", "type": "github", "config": {}}))
        # 通用来源 Token 允许为空：留空由服务端在保存时自动生成
        self.assertIsNone(server.source_error(
            {"name": "g2", "type": "generic", "config": {}}))
        self.assertIsNotNone(server.source_error(
            {"name": "g2", "type": "gitlab", "config": {}}))

    def test_multiple_github_sources_distinct_secret(self):
        body = b'{"a":1}'
        sig1 = "sha256=" + hmac.new(b"s1", body, hashlib.sha256).hexdigest()
        sig2 = "sha256=" + hmac.new(b"s2", body, hashlib.sha256).hexdigest()
        self.assertTrue(server._verify_github(
            {"X-Hub-Signature-256": sig1}, body, {"secret": "s1"}))
        self.assertTrue(server._verify_github(
            {"X-Hub-Signature-256": sig2}, body, {"secret": "s2"}))
        self.assertFalse(server._verify_github(
            {"X-Hub-Signature-256": sig1}, body, {"secret": "s2"}))

    def test_multi_channel_instances_notified(self):
        server.set_channels([
            {"name": "iphone", "type": "bark", "config": {"key": "k1"}},
            {"name": "pad", "type": "bark", "config": {"key": "k2"}},
        ])
        with mock.patch("server._push_channel", return_value=True) as push:
            self.assertTrue(server.notify("t", "b"))
        self.assertEqual(push.call_count, 2)
        self.assertEqual([c.args[0] for c in push.call_args_list], ["bark", "bark"])
        self.assertEqual({c.args[3]["key"] for c in push.call_args_list}, {"k1", "k2"})

    def test_save_remove_roundtrip(self):
        server.set_channels([])
        server.set_channels([{"name": "b1", "type": "bark", "config": {"key": "k"}}])
        self.assertEqual([c["name"] for c in server.get_channels()], ["b1"])
        server.set_channels([c for c in server.get_channels() if c["name"] != "b1"])
        self.assertEqual(server.get_channels(), [])

    @mock.patch("server._post_json", return_value={"code": 200})
    def test_bark_group_follows_source(self, post):
        """bark 推送 group 跟随信息源，而非写死 github。"""
        self.assertTrue(server._push_channel(
            "bark", "t", "b", {"key": "k", "url": "https://api.day.app"}, "demo-app"))
        self.assertEqual(post.call_args[0][1]["group"], "demo-app")

    @mock.patch("server._post_json", return_value={"code": 200})
    def test_bark_group_default_github(self, post):
        """无信息源时回退旧行为 group=github，subtitle 用渠道配置标签。"""
        self.assertTrue(server._push_channel(
            "bark", "t", "b", {"key": "k", "source": "github"}))
        self.assertEqual(post.call_args[0][1]["group"], "github")
        self.assertEqual(post.call_args[0][1]["subtitle"], "github")


class SubPathTest(unittest.TestCase):
    def tearDown(self):
        server.BASE_PATH = ""

    def test_root_unchanged(self):
        server.BASE_PATH = ""
        self.assertEqual(server.Handler.route("/hooks/github"), "/hooks/github")
        self.assertEqual(server.Handler.route("/ui"), "/ui")
        self.assertEqual(server.Handler.route("/"), "/")

    def test_with_base_path(self):
        server.BASE_PATH = "notify"
        self.assertEqual(server.Handler.route("/notify/hooks/github"), "/hooks/github")
        self.assertEqual(server.Handler.route("/notify/ui"), "/ui")
        self.assertEqual(server.Handler.route("/notify"), "/")
        self.assertIsNone(server.Handler.route("/hooks/github"))
        self.assertIsNone(server.Handler.route("/"))

    def test_trailing_slash(self):
        server.BASE_PATH = "notify"
        self.assertEqual(server.Handler.route("/notify/ui/"), "/ui")


class PanelGuardTest(unittest.TestCase):
    """HTTP 层：/api/settings 写接口的认证（fail-closed）与同源校验（CSRF）。"""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self._user, self._pwd = server.UI_AUTH_USER, server.UI_AUTH_PASS
        server.UI_AUTH_USER, server.UI_AUTH_PASS = PANEL_USER, PANEL_PWD

    def tearDown(self):
        server.UI_AUTH_USER, server.UI_AUTH_PASS = self._user, self._pwd

    def _post_settings(self, payload, headers=None, auth=True):
        body = json.dumps(payload).encode()
        h = server.Handler.__new__(server.Handler)
        h.path = "/api/settings"
        h.headers = {"Content-Length": str(len(body))}
        if auth:
            h.headers.update(AUTH_HEADER)
        h.headers.update(headers or {})
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses = []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = mock.Mock()
        h.end_headers = mock.Mock()
        h.do_POST()
        try:
            data = json.loads(h.wfile.getvalue().decode())
        except ValueError:
            data = {}
        return statuses, data

    def test_missing_credentials_401(self):
        """已配置凭据时，写接口未带凭据返回 401。"""
        statuses, _ = self._post_settings({"key": "event.watch", "value": 0}, auth=False)
        self.assertIn(401, statuses)

    def test_unconfigured_credentials_503(self):
        """面板未配置凭据时写接口返回 503（fail-closed），而非放行。"""
        server.UI_AUTH_USER = server.UI_AUTH_PASS = ""
        statuses, d = self._post_settings({"key": "event.watch", "value": 0})
        self.assertIn(503, statuses)
        self.assertIn("面板未配置登录凭据", d["error"])

    def test_cross_origin_rejected(self):
        """带 Origin 且与 Host 不同源的写请求 → 403。"""
        statuses, _ = self._post_settings(
            {"key": "event.watch", "value": 0},
            headers={"Origin": "https://evil.example", "Host": "hub.example.com"})
        self.assertIn(403, statuses)

    def test_same_origin_and_no_origin_accepted(self):
        """同源或未带 Origin/Referer（脚本 / curl 调用）时按业务逻辑正常处理。"""
        payload = {"key": "event.watch", "value": 0}
        statuses, _ = self._post_settings(payload)
        self.assertIn(200, statuses)
        statuses, _ = self._post_settings(
            payload, headers={"Origin": "https://hub.example.com", "Host": "hub.example.com"})
        self.assertIn(200, statuses)


class GenericHookHTTPTest(unittest.TestCase):
    """HTTP 层：/hooks/generic 只接受标准 Authorization: Bearer；旧 X-Notify-Token 头不再接受。"""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self._prev_src = server.get_sources()
        self._prev_base = server.BASE_PATH
        server.BASE_PATH = ""   # 根路径模式直测 /hooks/generic

    def tearDown(self):
        server.set_sources(self._prev_src)
        server.BASE_PATH = self._prev_base

    def _post_hook(self, headers, body, content_length=None):
        h = server.Handler.__new__(server.Handler)
        h.path = "/hooks/generic"
        h.headers = {**headers,
                     "Content-Length": str(len(body) if content_length is None else content_length)}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses = []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = mock.Mock()
        h.end_headers = mock.Mock()
        h.do_POST()
        try:
            data = json.loads(h.wfile.getvalue().decode())
        except ValueError:
            data = {}
        return statuses, data

    @mock.patch("server._push_channel", return_value=True)
    def test_bearer_accepted(self, push):
        server.set_sources([{"name": "generic", "type": "generic", "config": {"token": "tok"}}])
        statuses, d = self._post_hook({"Authorization": "Bearer tok"}, b'{"title":"t","body":"b"}')
        self.assertIn(200, statuses)
        self.assertTrue(d["ok"])
        self.assertTrue(d["pushed"])

    @mock.patch("server._push_channel", return_value=True)
    def test_legacy_header_rejected(self, push):
        """旧 X-Notify-Token 头已移除：带该头的请求一律 401，不再有兼容回退。"""
        server.set_sources([{"name": "generic", "type": "generic", "config": {"token": "tok"}}])
        statuses, d = self._post_hook({"X-Notify-Token": "tok"}, b'{"title":"t","body":"b"}')
        self.assertIn(401, statuses)

    def test_wrong_token_rejected(self):
        server.set_sources([{"name": "generic", "type": "generic", "config": {"token": "tok"}}])
        statuses, d = self._post_hook({"Authorization": "Bearer bad"}, b'{"title":"t","body":"b"}')
        self.assertIn(401, statuses)
        self.assertFalse(d["ok"])

    def test_missing_token_rejected(self):
        server.set_sources([{"name": "generic", "type": "generic", "config": {"token": "tok"}}])
        statuses, _ = self._post_hook({}, b'{"title":"t","body":"b"}')
        self.assertIn(401, statuses)

    def test_body_too_large_rejected(self):
        """Content-Length 超过 MAX_BODY_BYTES → 413（在读体之前就拒绝）。"""
        server.set_sources([{"name": "generic", "type": "generic", "config": {"token": "tok"}}])
        statuses, d = self._post_hook({"Authorization": "Bearer tok"}, b"{}",
                                      content_length=server.MAX_BODY_BYTES + 1)
        self.assertIn(413, statuses)
        self.assertFalse(d["ok"])

    def test_negative_content_length_rejected(self):
        """非法 / 负数 Content-Length → 400。"""
        server.set_sources([{"name": "generic", "type": "generic", "config": {"token": "tok"}}])
        statuses, d = self._post_hook({"Authorization": "Bearer tok"}, b"{}", content_length=-1)
        self.assertIn(400, statuses)
        self.assertFalse(d["ok"])


class LoginHTTPTest(unittest.TestCase):
    """HTTP 层：/login 用户名/密码首尾空格容错（strip 后比对）。"""

    def setUp(self):
        self._u, self._p = server.UI_AUTH_USER, server.UI_AUTH_PASS
        server.UI_AUTH_USER, server.UI_AUTH_PASS = "admin", "secret"
        self._prev_base = server.BASE_PATH
        server.BASE_PATH = ""

    def tearDown(self):
        server.UI_AUTH_USER, server.UI_AUTH_PASS = self._u, self._p
        server.BASE_PATH = self._prev_base

    def _post_login(self, user, pwd):
        body = urllib.parse.urlencode({"username": user, "password": pwd}).encode()
        h = server.Handler.__new__(server.Handler)
        h.path = "/login"
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses, locations = [], []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = lambda k, v: locations.append(v) if k == "Location" else None
        h.end_headers = mock.Mock()
        h.do_POST()
        return statuses, locations

    def test_login_ok(self):
        statuses, locs = self._post_login("admin", "secret")
        self.assertIn(302, statuses)
        self.assertIn("/ui", locs[-1])

    def test_login_strips_whitespace(self):
        """首尾空格/换行容错：strip 后正确密码可登录。"""
        statuses, locs = self._post_login(" admin ", " secret ")
        self.assertIn(302, statuses)
        self.assertIn("/ui", locs[-1])

    def test_login_wrong_rejected(self):
        statuses, locs = self._post_login("admin", "wrong")
        self.assertIn(302, statuses)
        self.assertIn("error=1", locs[-1])

    def _post_login_cookies(self, headers=None):
        """登录并返回 (statuses, Set-Cookie 值列表)，用于检查 Cookie 属性。"""
        body = urllib.parse.urlencode({"username": "admin", "password": "secret"}).encode()
        h = server.Handler.__new__(server.Handler)
        h.path = "/login"
        h.headers = {"Content-Length": str(len(body)), **(headers or {})}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses, cookies = [], []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = lambda k, v: cookies.append(v) if k == "Set-Cookie" else None
        h.end_headers = mock.Mock()
        h.do_POST()
        return statuses, cookies

    def test_login_cookie_secure_behind_https_proxy(self):
        """经 HTTPS 反代（X-Forwarded-Proto: https）登录时 Cookie 带 Secure。"""
        statuses, cookies = self._post_login_cookies({"X-Forwarded-Proto": "https"})
        self.assertIn(302, statuses)
        self.assertIn("Secure", cookies[-1])

    def test_login_cookie_no_secure_without_https(self):
        """纯 HTTP 访问（无 X-Forwarded-Proto）时 Cookie 不带 Secure。"""
        statuses, cookies = self._post_login_cookies()
        self.assertIn(302, statuses)
        self.assertNotIn("Secure", cookies[-1])


class RootPathTest(unittest.TestCase):
    """子域名根路径部署：GET / 302 → /ui（未登录时再 302 /login）；子路径模式跟随前缀。"""

    def _get_root(self, path, base=""):
        prev = server.BASE_PATH
        server.BASE_PATH = base
        try:
            h = server.Handler.__new__(server.Handler)
            h.path = path
            h.headers = {}
            h.wfile = io.BytesIO()
            h.client_address = ("127.0.0.1", 0)
            statuses = []
            locations = []
            h.send_response = lambda s: statuses.append(s)
            h.send_header = lambda k, v: locations.append(v) if k == "Location" else None
            h.end_headers = mock.Mock()
            h.do_GET()
            return statuses, locations
        finally:
            server.BASE_PATH = prev

    def test_root_redirects_to_ui(self):
        statuses, locations = self._get_root("/")
        self.assertIn(302, statuses)
        self.assertEqual(locations, ["/ui"])

    def test_root_redirect_respects_base_path(self):
        statuses, locations = self._get_root("/notify", base="notify")
        self.assertIn(302, statuses)
        self.assertEqual(locations, ["/notify/ui"])


class SourceCredentialTest(unittest.TestCase):
    """Token/Secret 服务端管理：新建留空自动生成 / 编辑保留原值 / regen 支持任意来源类型。"""

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self._prev = server.get_sources()
        server.set_sources([])

    def tearDown(self):
        server.set_sources(self._prev)

    def _post(self, payload):
        body = json.dumps(payload).encode()
        h = server.Handler.__new__(server.Handler)
        h.path = "/api/settings"
        h.headers = {**AUTH_HEADER, "Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.client_address = ("127.0.0.1", 0)
        statuses = []
        h.send_response = lambda s: statuses.append(s)
        h.send_header = mock.Mock()
        h.end_headers = mock.Mock()
        h.do_POST()
        data = {}
        try:
            data = json.loads(h.wfile.getvalue().decode())
        except ValueError:
            pass
        return statuses, data

    def _save(self, name, typ, config=None, original_name=""):
        return self._post({"action": "source/save",
                           "item": {"name": name, "type": typ, "config": config or {}},
                           "original_name": original_name})

    def test_save_github_empty_secret_autogen(self):
        """github 空 secret 保存自动生成并入库。"""
        statuses, d = self._save("gh-src", "github")
        self.assertIn(200, statuses)
        sec = d["item"]["config"]["secret"]
        self.assertTrue(sec)
        self.assertEqual(len(sec), 32)   # secrets.token_urlsafe(24) → 32 字符
        saved = next(s for s in server.get_sources() if s["name"] == "gh-src")
        self.assertEqual(saved["config"]["secret"], sec)

    def test_save_generic_empty_token_autogen(self):
        """generic 空 token 保存自动生成并入库。"""
        statuses, d = self._save("gen-src", "generic")
        self.assertIn(200, statuses)
        tok = d["item"]["config"]["token"]
        self.assertTrue(tok)
        saved = next(s for s in server.get_sources() if s["name"] == "gen-src")
        self.assertEqual(saved["config"]["token"], tok)

    def test_edit_keeps_existing_secret(self):
        """编辑（未携带 secret）保留原值不覆盖，其它字段正常更新。"""
        statuses, d = self._save("kept", "github")
        self.assertIn(200, statuses)
        orig = d["item"]["config"]["secret"]
        statuses, d2 = self._save("kept", "github", {"channels": []}, original_name="kept")
        self.assertIn(200, statuses)
        self.assertEqual(d2["item"]["config"]["secret"], orig)
        saved = next(s for s in server.get_sources() if s["name"] == "kept")
        self.assertEqual(saved["config"]["channels"], [])
        self.assertEqual(saved["config"]["secret"], orig)

    def test_edit_rename_keeps_existing_token(self):
        """编辑并改名时原实例的 token 跟随保留。"""
        statuses, d = self._save("old-name", "generic")
        orig = d["item"]["config"]["token"]
        statuses, d2 = self._save("new-name", "generic", {}, original_name="old-name")
        self.assertIn(200, statuses)
        self.assertEqual(d2["item"]["config"]["token"], orig)
        self.assertIsNone(next((s for s in server.get_sources() if s["name"] == "old-name"), None))

    def test_regen_github_secret_changes(self):
        """source/regen 支持 github：secret 重新生成且入库。"""
        statuses, d = self._save("rg-gh", "github")
        self.assertIn(200, statuses)
        orig = d["item"]["config"]["secret"]
        statuses, d = self._post({"action": "source/regen", "name": "rg-gh"})
        self.assertIn(200, statuses)
        self.assertTrue(d["item"]["config"]["secret"])
        self.assertNotEqual(d["item"]["config"]["secret"], orig)
        saved = next(s for s in server.get_sources() if s["name"] == "rg-gh")
        self.assertEqual(saved["config"]["secret"], d["item"]["config"]["secret"])

    def test_regen_generic_token_changes(self):
        """source/regen 支持 generic：token 重新生成。"""
        statuses, d = self._save("rg-gen", "generic")
        self.assertIn(200, statuses)
        orig = d["item"]["config"]["token"]
        statuses, d = self._post({"action": "source/regen", "name": "rg-gen"})
        self.assertIn(200, statuses)
        self.assertTrue(d["item"]["config"]["token"])
        self.assertNotEqual(d["item"]["config"]["token"], orig)

    def test_regen_unknown_source_404(self):
        statuses, _ = self._post({"action": "source/regen", "name": "missing"})
        self.assertIn(404, statuses)

    def test_source_error_generic_empty_token_legal(self):
        """空 token 的 generic 来源校验合法；未知类型等其它校验仍生效。"""
        self.assertIsNone(server.source_error({"name": "g", "type": "generic", "config": {}}))
        self.assertIsNone(server.source_error({"name": "g", "type": "github", "config": {}}))
        self.assertIsNotNone(server.source_error({"name": "g", "type": "gitlab", "config": {}}))


if __name__ == "__main__":
    unittest.main()
