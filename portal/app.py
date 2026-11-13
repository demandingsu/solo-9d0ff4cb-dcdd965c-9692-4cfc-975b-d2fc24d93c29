"""本地示例运营门户（演示用，非生产代码）。

免登录的时刻编辑门户，供“接驳时刻勘误回填”演示与下游采集：

  * 首页有 ``a.edit-entry`` 指向编辑第 1 页；
  * 编辑页分页展示时刻表，每页一个表单：隐藏字段 ``_token``（会话令牌，
    与 portal_sid Cookie 对应）与 ``_revision``（页修订号），每行一个
    ``clock-<行ID>`` 文本框；页脚 ``a.next-page`` 指向下一页；
  * 保存（POST /save/page-N）校验令牌与修订号：令牌不符 403，修订过期 409；
  * /demo 演示控制台：
      - 武装并发改表：下一次保存该页时，先由“他人”修改该页末行并递增
        修订号，再拒绝本次提交（409）——可叠加武装多次；
      - 武装保存后响应中断：下一次保存正常生效，但响应中途断开；
      - 立即以他人身份改表、重置全部数据。
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid

from flask import Flask, Response, abort, redirect, render_template_string, request, url_for

app = Flask(__name__)
PAGE_SIZE = 6

# (行ID, 站名, 车次号, 服务日, 时刻)
INITIAL_ROWS: list[tuple[int, str, str, str, str]] = [
    (1, "滨河园区", "N201", "工作日", "22:30"),
    (2, "东门枢纽", "N201", "工作日", "22:55"),
    (3, "中央厂区", "N201", "工作日", "23:40"),
    (4, "夜班宿舍", "N201", "工作日", "00:10"),
    (5, "滨河园区", "N202", "每日", "23:15"),
    (6, "东门枢纽", "N202", "每日", "23:40"),
    (7, "中央厂区", "N202", "每日", "24:25"),
    (8, "夜班宿舍", "N202", "每日", "24:55"),
    (9, "滨河园区", "N203", "工作日", "24:00"),
    (10, "东门枢纽", "N203", "工作日", "24:25"),
    (11, "中央厂区", "N203", "工作日", "25:10"),
    (12, "夜班宿舍", "N203", "工作日", "25:40"),
    (13, "滨河园区", "N204", "周末", "23:50"),
    (14, "东门枢纽", "N204", "周末", "00:15"),
    (15, "中央厂区", "N204", "周末", "01:00"),
    (16, "夜班宿舍", "N204", "周末", "01:30"),
    (17, "滨河园区", "N205", "每日", "24:40"),
    (18, "东门枢纽", "N205", "每日", "25:05"),
]

LOCK = threading.Lock()
ROWS: list[dict] = []
REVISIONS: dict[int, int] = {}        # 页号 -> 当前修订号（缺省为 1）
ARMED_CONFLICT: dict[int, int] = {}   # 页号 -> 待触发的并发冲突次数
ARMED_DROP: dict[int, int] = {}       # 页号 -> 待触发的响应中断次数


def reset_state() -> None:
    """恢复初始时刻数据、修订号与演示武装状态。"""
    with LOCK:
        ROWS[:] = [
            dict(zip(("id", "station", "trip", "kind", "clock"), row))
            for row in INITIAL_ROWS
        ]
        REVISIONS.clear()
        ARMED_CONFLICT.clear()
        ARMED_DROP.clear()


reset_state()


def _total_pages() -> int:
    return (len(ROWS) + PAGE_SIZE - 1) // PAGE_SIZE


def _page_rows(page: int) -> list[dict]:
    return ROWS[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]


def _revision(page: int) -> int:
    return REVISIONS.get(page, 1)


def _token_for(sid: str) -> str:
    return hashlib.sha256(f"portal-demo::{sid}".encode("utf-8")).hexdigest()[:20]


def _ensure_sid() -> tuple[str, bool]:
    """返回 (会话ID, 是否需要新写 Cookie)。免登录：首次访问即发放。"""
    sid = request.cookies.get("portal_sid")
    return (sid, False) if sid else (uuid.uuid4().hex, True)


def _bump_clock(clock: str) -> str:
    """“他人并发修改”用：时刻 +7 分钟（48 小时制内回绕）。"""
    match = re.match(r"^\s*(\d{1,2}):([0-5]\d)\s*$", clock or "")
    if not match:
        return "07:07"
    total = (int(match.group(1)) * 60 + int(match.group(2)) + 7) % (48 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _apply_concurrent_edit(page: int) -> None:
    """模拟另一名数据员：修改该页末行时刻并递增页修订号（须在锁内调用）。"""
    rows = _page_rows(page)
    if rows:
        rows[-1]["clock"] = _bump_clock(rows[-1]["clock"])
    REVISIONS[page] = _revision(page) + 1


def _broken_response() -> Response:
    """保存已生效，但响应流中途断开（客户端会看到连接提前结束）。"""
    def gen():
        yield "<!doctype html><html lang='zh-CN'><body><p>保存已生效，但响应在此中断（演示）……</p>".encode("utf-8")
        raise RuntimeError("演示：保存后响应中断")

    return Response(gen(), content_type="text/html; charset=utf-8")


BASE_CSS = """
body{font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;margin:32px;color:#1f2933;}
table{border-collapse:collapse;} th,td{border:1px solid #888;padding:6px 12px;}
a{color:#1d4ed8;} .muted{color:#667085;font-size:13px;}
button{background:#1d4ed8;color:#fff;border:0;padding:6px 14px;border-radius:6px;cursor:pointer;}
.banner{background:#ecfdf3;border:1px solid #12b76a;border-radius:8px;padding:10px 14px;margin:12px 0;}
.warn{background:#fffaeb;border-color:#f79009;} .err{background:#fef3f2;border-color:#f04438;}
.nav{margin-top:14px;} input[type=text]{padding:4px 6px;}
"""

HOME_HTML = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>示例运营门户</title><style>{{ css }}</style></head>
<body><h1>示例运营门户（免登录）</h1>
<p>夜班接驳时刻的编辑门户：数据分页展示，每页一个编辑表单，保存时校验
会话令牌与页修订号，过期修订会被拒绝（HTTP 409）。</p>
<p><a class="edit-entry" href="{{ url_for('edit_page', page=1) }}">→ 进入时刻编辑（第 1 页）</a></p>
<p><a href="{{ url_for('demo') }}">→ 演示控制台（并发改表 / 保存后响应中断 / 重置数据）</a></p>
<p class="muted">配合「接驳时刻勘误回填」使用：在回填页把门户 URL 填为本站地址。</p>
</body></html>
"""

EDIT_HTML = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>时刻编辑 第 {{ page }} 页</title><style>{{ css }}</style></head>
<body>
<h1>时刻编辑（第 {{ page }} / {{ total_pages }} 页）</h1>
{% if saved is not none %}<div class="banner">已保存本页（{{ saved }} 行有改动），当前修订号 {{ revision }}。</div>{% endif %}
<p class="muted">当前页修订号：<strong>{{ revision }}</strong>；会话令牌：<code>{{ token[:8] }}…</code>
（隐藏字段随表单自动提交）。</p>
<form method="post" action="{{ url_for('save_page', page=page) }}">
  <input type="hidden" name="_token" value="{{ token }}">
  <input type="hidden" name="_revision" value="{{ revision }}">
  <table>
    <tr><th>站名</th><th>车次号</th><th>服务日</th><th>时刻</th></tr>
    {% for row in rows %}
    <tr data-row-id="{{ row.id }}" data-station="{{ row.station }}"
        data-trip="{{ row.trip }}" data-service="{{ row.kind }}">
      <td>{{ row.station }}</td><td>{{ row.trip }}</td><td>{{ row.kind }}</td>
      <td><input type="text" name="clock-{{ row.id }}" value="{{ row.clock }}" size="7"></td>
    </tr>
    {% endfor %}
  </table>
  <p><button type="submit">保存本页</button></p>
</form>
<div class="nav">
  {% if page > 1 %}<a href="{{ url_for('edit_page', page=page-1) }}">← 上一页</a>{% endif %}
  {% if page < total_pages %}
    <a class="next-page" href="{{ url_for('edit_page', page=page+1) }}">下一页 →</a>
  {% endif %}
  <p><a href="{{ url_for('home') }}">返回首页</a> · <a href="{{ url_for('demo') }}">演示控制台</a></p>
</div>
</body></html>
"""

CONFLICT_HTML = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>修订号过期</title><style>{{ css }}</style></head>
<body>
<div class="banner err"><strong>保存被拒绝（409）：页修订号已过期。</strong><br>
你提交的是修订号 {{ submitted }}，但第 {{ page }} 页当前修订号已是 {{ current }}——
他人可能已并发修改本页。请重新打开编辑页，基于最新数据再提交。</div>
<p><a href="{{ url_for('edit_page', page=page) }}">→ 重新打开第 {{ page }} 页编辑表单</a></p>
</body></html>
"""

DEMO_HTML = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>演示控制台</title><style>{{ css }}</style></head>
<body>
<h1>演示控制台</h1>
<p>在这里武装一次性故障，再到「接驳时刻勘误回填」页提交涉及该页的勘误，即可观察回填端的表现：</p>
<ul>
  <li><strong>并发改表</strong>：下一次保存该页时，门户先以“他人”身份把该页<strong>末行</strong>时刻 +7 分钟
      并递增修订号，再拒绝本次提交（409）。回填端应重读该页并按同一规则重试一次。</li>
  <li><strong>保存后响应中断</strong>：下一次保存正常生效，但响应中途断开。回填端应只做回读核验，不自动重提。</li>
  <li><strong>立即并发改表</strong>：立刻以“他人”身份改该页末行（用于手工演示：先打开编辑页，再点这里，
      然后提交刚才打开的表单，会看到 409）。</li>
</ul>
<table>
  <tr><th>分页</th><th>当前修订号</th><th>已武装</th><th>操作</th></tr>
  {% for p in pages %}
  <tr>
    <td><a href="{{ url_for('edit_page', page=p.no) }}">第 {{ p.no }} 页</a></td>
    <td>{{ p.revision }}</td>
    <td>{% if p.conflict %}并发改表×{{ p.conflict}} {% endif %}{% if p.drop %}响应中断×{{ p.drop }}{% endif %}
        {% if not p.conflict and not p.drop %}—{% endif %}</td>
    <td>
      <form style="display:inline" method="post" action="{{ url_for('demo_arm_conflict', page=p.no) }}"><button>武装并发改表</button></form>
      <form style="display:inline" method="post" action="{{ url_for('demo_arm_drop', page=p.no) }}"><button>武装响应中断</button></form>
      <form style="display:inline" method="post" action="{{ url_for('demo_concurrent_edit', page=p.no) }}"><button>立即并发改表</button></form>
    </td>
  </tr>
  {% endfor %}
</table>
<p>
  <form method="post" action="{{ url_for('demo_reset') }}"><button>重置全部数据与修订号</button></form>
</p>
<p><a href="{{ url_for('home') }}">返回首页</a></p>
</body></html>
"""


@app.route("/")
def home():
    sid, is_new = _ensure_sid()
    resp = app.make_response(render_template_string(HOME_HTML, css=BASE_CSS))
    if is_new:
        resp.set_cookie("portal_sid", sid)
    return resp


@app.route("/edit/page-<int:page>")
def edit_page(page: int):
    if page < 1 or page > _total_pages():
        abort(404)
    sid, is_new = _ensure_sid()
    saved = request.args.get("saved")
    with LOCK:
        rows = [dict(row) for row in _page_rows(page)]
        revision = _revision(page)
    resp = app.make_response(render_template_string(
        EDIT_HTML,
        css=BASE_CSS,
        page=page,
        total_pages=_total_pages(),
        rows=rows,
        revision=revision,
        token=_token_for(sid),
        saved=int(saved) if saved and saved.isdigit() else None,
    ))
    if is_new:
        resp.set_cookie("portal_sid", sid)
    return resp


@app.route("/save/page-<int:page>", methods=["POST"])
def save_page(page: int):
    if page < 1 or page > _total_pages():
        abort(404)
    sid = request.cookies.get("portal_sid", "")
    if not sid or request.form.get("_token", "") != _token_for(sid):
        abort(403, description="表单令牌无效或会话已过期，请回到编辑页重新提交。")

    with LOCK:
        # 武装的并发改表：先由“他人”修改本页末行并递增修订号，使本次提交过期
        if ARMED_CONFLICT.get(page, 0) > 0:
            ARMED_CONFLICT[page] -= 1
            _apply_concurrent_edit(page)

        current = _revision(page)
        submitted = request.form.get("_revision", "")
        if submitted != str(current):
            return render_template_string(
                CONFLICT_HTML, css=BASE_CSS, page=page,
                current=current, submitted=submitted or "（空）",
            ), 409

        changed = 0
        for row in _page_rows(page):
            field_name = f"clock-{row['id']}"
            if field_name in request.form:
                value = request.form[field_name].strip()
                if value and value != row["clock"]:
                    row["clock"] = value
                    changed += 1
        REVISIONS[page] = current + 1

        drop = ARMED_DROP.get(page, 0) > 0
        if drop:
            ARMED_DROP[page] -= 1

    if drop:
        # 保存已生效，但响应中途断开
        return _broken_response()
    return redirect(url_for("edit_page", page=page, saved=changed))


@app.route("/demo")
def demo():
    sid, is_new = _ensure_sid()
    with LOCK:
        pages = [
            {
                "no": no,
                "revision": _revision(no),
                "conflict": ARMED_CONFLICT.get(no, 0),
                "drop": ARMED_DROP.get(no, 0),
            }
            for no in range(1, _total_pages() + 1)
        ]
    resp = app.make_response(render_template_string(DEMO_HTML, css=BASE_CSS, pages=pages))
    if is_new:
        resp.set_cookie("portal_sid", sid)
    return resp


def _valid_page_or_404(page: int) -> None:
    if page < 1 or page > _total_pages():
        abort(404)


@app.route("/demo/arm-conflict/page-<int:page>", methods=["POST"])
def demo_arm_conflict(page: int):
    _valid_page_or_404(page)
    with LOCK:
        ARMED_CONFLICT[page] = ARMED_CONFLICT.get(page, 0) + 1
    return redirect(url_for("demo"))


@app.route("/demo/arm-drop/page-<int:page>", methods=["POST"])
def demo_arm_drop(page: int):
    _valid_page_or_404(page)
    with LOCK:
        ARMED_DROP[page] = ARMED_DROP.get(page, 0) + 1
    return redirect(url_for("demo"))


@app.route("/demo/concurrent-edit/page-<int:page>", methods=["POST"])
def demo_concurrent_edit(page: int):
    _valid_page_or_404(page)
    with LOCK:
        _apply_concurrent_edit(page)
    return redirect(url_for("demo"))


@app.route("/demo/reset", methods=["POST"])
def demo_reset():
    reset_state()
    return redirect(url_for("demo"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, threaded=True)
