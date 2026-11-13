"""勘误回填端到端测试：真实门户（werkzeug 服务）+ 回填客户端 + Flask 路由。

覆盖：正常回填、已是新时刻跳过、预检冲突、页修订冲突重试、保存后响应中断、
双重冲突后未完成、门户读不到待核实、无匹配行、勘误表解析。

仅在安装了依赖（Flask/requests/bs4）后可运行：
    python -m unittest tests.test_backfill
"""

import importlib.util
import threading
import unittest
from pathlib import Path

import requests
from werkzeug.serving import make_server

from commuter.app import app as commuter_app
from commuter.backfill import (
    STATUS_CONFLICT,
    STATUS_DONE,
    STATUS_UNDONE,
    STATUS_UNKNOWN,
    parse_corrections,
    run_backfill,
)

PORTAL_DIR = Path(__file__).resolve().parents[2] / "portal"


def _load_portal_module():
    """按路径加载门户应用（避免与 example-site 的 app 模块重名）。"""
    spec = importlib.util.spec_from_file_location("portal_app", PORTAL_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


portal_module = _load_portal_module()


class _Server:
    def __init__(self, flask_app, port):
        self.httpd = make_server("127.0.0.1", port, flask_app, threaded=True)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()


class BackfillLiveTests(unittest.TestCase):
    """真实 HTTP 门户上的回填流程测试。"""

    @classmethod
    def setUpClass(cls):
        cls.server = _Server(portal_module.app, 5052)
        cls.server.start()
        cls.base = "http://127.0.0.1:5052/"
        requests.get(cls.base, timeout=5).raise_for_status()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        requests.post(f"{self.base}demo/reset", timeout=5).raise_for_status()

    # ---- 门户辅助 ----

    def _arm_conflict(self, page, times=1):
        for _ in range(times):
            requests.post(f"{self.base}demo/arm-conflict/page-{page}", timeout=5).raise_for_status()

    def _arm_drop(self, page):
        requests.post(f"{self.base}demo/arm-drop/page-{page}", timeout=5).raise_for_status()

    def _page_html(self, page):
        return requests.get(f"{self.base}edit/page-{page}", timeout=5).text

    def _revision_of(self, page):
        import re

        html = self._page_html(page)
        match = re.search(r'name="_revision" value="(\d+)"', html)
        self.assertIsNotNone(match, f"第 {page} 页未找到修订号")
        return int(match.group(1))

    def _clock_of(self, row_id):
        import re

        for page in (1, 2, 3):
            html = self._page_html(page)
            match = re.search(rf'name="clock-{row_id}" value="([^"]*)"', html)
            if match:
                return match.group(1)
        self.fail(f"门户中找不到行 clock-{row_id}")

    def _run(self, text):
        corrections, errors = parse_corrections(text)
        self.assertEqual(errors, [])
        return run_backfill(self.base, corrections)

    # ---- 测试用例 ----

    def test_happy_path_applies_and_preserves_unrelated_rows(self):
        report = self._run(
            "滨河园区,N201,工作日,22:30,22:35\n"
            "中央厂区,N202,每日,24:25,24:30\n"
            "夜班宿舍,N204,周末,01:30,01:35\n"
        )
        self.assertEqual(report.errors, [])
        self.assertTrue(report.verified)
        self.assertEqual([it.status for it in report.items], [STATUS_DONE] * 3)
        # 来源页逐项给出，且覆盖三页
        self.assertIn("/edit/page-1", report.items[0].source_page)
        self.assertIn("/edit/page-2", report.items[1].source_page)
        self.assertIn("/edit/page-3", report.items[2].source_page)
        # 门户数据已更新
        self.assertEqual(self._clock_of(1), "22:35")
        self.assertEqual(self._clock_of(7), "24:30")
        self.assertEqual(self._clock_of(16), "01:35")
        # 无关行原样保留
        self.assertEqual(self._clock_of(2), "22:55")
        self.assertEqual(self._clock_of(18), "25:05")
        # 三页各提交一次
        self.assertEqual(sum("已提交" in line for line in report.submit_log), 3)

    def test_already_new_skips_and_does_not_submit(self):
        self._run("滨河园区,N201,工作日,22:30,22:35\n")
        revision_before = self._revision_of(1)
        report = self._run("滨河园区,N201,工作日,22:30,22:35\n")
        self.assertEqual(report.items[0].status, STATUS_DONE)
        self.assertTrue(any("已是新时刻" in note for note in report.items[0].notes))
        self.assertTrue(any("没有待改行，未提交" in line for line in report.submit_log))
        # 没有待改行便不提交：修订号不变
        self.assertEqual(self._revision_of(1), revision_before)

    def test_other_value_reports_conflict_and_keeps_portal(self):
        report = self._run("滨河园区,N201,工作日,20:00,20:05\n")
        item = report.items[0]
        self.assertEqual(item.status, STATUS_CONFLICT)
        self.assertEqual(item.current_clock, "22:30")
        self.assertTrue(any("预检冲突" in note for note in item.notes))
        self.assertTrue(any("没有待改行，未提交" in line for line in report.submit_log))
        self.assertEqual(self._clock_of(1), "22:30")

    def test_revision_conflict_retried_once_and_succeeds(self):
        self._arm_conflict(1)
        report = self._run("滨河园区,N201,工作日,22:30,22:35\n")
        self.assertTrue(any("409" in line for line in report.submit_log))
        self.assertTrue(any("已提交" in line for line in report.submit_log))
        self.assertEqual(report.items[0].status, STATUS_DONE)
        self.assertEqual(self._clock_of(1), "22:35")
        # 并发修改落在该页末行（clock-6：23:40 → 23:47），且未被回填覆盖
        self.assertEqual(self._clock_of(6), "23:47")

    def test_revision_conflict_on_target_row_reports_conflict(self):
        # 并发修改的正是勘误目标行（第 1 页末行）：重读后现值既非原时刻也非新时刻
        self._arm_conflict(1)
        report = self._run("东门枢纽,N202,每日,23:40,23:45\n")
        item = report.items[0]
        self.assertEqual(item.status, STATUS_CONFLICT)
        self.assertEqual(item.current_clock, "23:47")
        self.assertEqual(self._clock_of(6), "23:47")

    def test_interrupted_response_verified_without_resubmit(self):
        self._arm_drop(2)
        report = self._run("中央厂区,N202,每日,24:25,24:30\n")
        self.assertTrue(any("中断" in line for line in report.submit_log))
        # 保存实际已生效：回读确认完成
        self.assertEqual(report.items[0].status, STATUS_DONE)
        self.assertEqual(self._clock_of(7), "24:30")
        # 不自动重提：第 2 页修订号只递增一次（1 → 2）
        self.assertEqual(self._revision_of(2), 2)

    def test_double_revision_conflict_gives_up_and_marks_undone(self):
        self._arm_conflict(1, times=2)  # 首次提交与重试都被顶掉
        report = self._run("滨河园区,N201,工作日,22:30,22:35\n")
        self.assertTrue(any("不再重试" in line for line in report.submit_log))
        self.assertEqual(report.items[0].status, STATUS_UNDONE)
        self.assertEqual(self._clock_of(1), "22:30")

    def test_unreachable_portal_marks_unknown(self):
        corrections, errors = parse_corrections("滨河园区,N201,工作日,22:30,22:35\n")
        self.assertEqual(errors, [])
        report = run_backfill("http://127.0.0.1:59999/", corrections)
        self.assertTrue(report.errors)
        self.assertEqual([it.status for it in report.items], [STATUS_UNKNOWN])

    def test_row_not_found_is_conflict(self):
        report = self._run("不存在的站,N999,每日,22:00,22:05\n")
        self.assertEqual(report.items[0].status, STATUS_CONFLICT)
        self.assertTrue(any("未找到匹配行" in note for note in report.items[0].notes))

    def test_flask_route_renders_report(self):
        client = commuter_app.test_client()
        html = client.post(
            "/backfill",
            data={
                "portal_url": self.base,
                "corrections": "滨河园区,N201,工作日,22:30,22:35\n",
            },
        ).get_data(as_text=True)
        self.assertIn("确认完成", html)
        self.assertIn("/edit/page-1", html)
        self.assertIn("页面提交记录", html)


class ParseCorrectionsTests(unittest.TestCase):
    def test_valid_lines_and_comments(self):
        items, errors = parse_corrections(
            "# 注释行\n"
            "\n"
            "滨河园区,N201,工作日,22:30,22:35\n"
            "东门枢纽，N202，每日，23:40，23:45\n"  # 中文逗号
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1].service_kind, "每日")
        self.assertEqual(items[0].line_no, 3)

    def test_bad_lines(self):
        items, errors = parse_corrections(
            "只有三列,N201,工作日\n"
            "滨河园区,N201,节假日,22:30,22:35\n"
            "滨河园区,N201,工作日,abc,22:35\n"
            "滨河园区,N201,工作日,22:30,22:30\n"
            "滨河园区,N201,工作日,22:30,22:35\n"   # 唯一合法行
            "滨河园区,N201,工作日,22:30,22:36\n"   # 与上一行定位重复
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].line_no, 5)
        self.assertEqual(len(errors), 5)


if __name__ == "__main__":
    unittest.main()
