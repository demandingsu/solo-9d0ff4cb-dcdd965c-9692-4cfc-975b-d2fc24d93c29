"""接驳时刻勘误回填：把核准的勘误写回运营门户，并回读核验。

作业流程（对应站务数据员的回填动作）：

  1. 沿分页读取门户整页编辑表单（同一会话，表单含隐藏令牌与页修订号）；
  2. 提交前逐行核对：门户现值仍为原时刻才改，已是新时刻则跳过，其他值报冲突；
  3. 整页提交（无关行按现值原样带回）；没有待改行的页不提交；
  4. 页修订冲突（HTTP 409）后重读该页，按同一规则最多重试一次；
  5. 提交后回读核验：新时刻=确认完成，原时刻=未完成，其他值=冲突，读不到=待核实；
     提交响应中断也只核验，不自动重提。

该模块不依赖 Flask，方便单元测试；Web 入口见 :mod:`commuter.app` 的 ``/backfill``。

门户页面结构约定（示例门户见仓库 ``portal/``）：
  * 门户首页含 ``a.edit-entry`` 指向编辑第 1 页（若 URL 本身已是编辑页则直接用）；
  * 编辑页含一个带隐藏字段 ``_token`` / ``_revision`` 的 ``form``，行元素为
    ``tr[data-row-id][data-station][data-trip][data-service]``，行内时刻输入框
    名为 ``clock-<行ID>``；页脚 ``a.next-page`` 指向下一页，末页没有该链接。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .times import parse_clock

REQUEST_TIMEOUT = 10
MAX_PAGES = 20   # 防止异常门户造成无限翻页
MAX_ATTEMPTS = 2  # 首次提交 + 页修订冲突后最多重试一次

# 逐项核验状态
STATUS_DONE = "确认完成"      # 回读为新时刻
STATUS_UNDONE = "未完成"      # 回读仍为原时刻
STATUS_CONFLICT = "冲突"      # 回读为其他值，或门户中找不到该行
STATUS_UNKNOWN = "待核实"     # 回读取不到门户数据

_KIND_ALIASES = {
    "工作日": "工作日",
    "平日": "工作日",
    "周一至周五": "工作日",
    "weekday": "工作日",
    "周末": "周末",
    "双休": "周末",
    "周六周日": "周末",
    "weekend": "周末",
    "每日": "每日",
    "每天": "每日",
    "daily": "每日",
}


def _norm(text: str) -> str:
    return "".join((text or "").split()).lower()


def _norm_kind(text: str) -> str | None:
    return _KIND_ALIASES.get(_norm(text))


def _clock_eq(a: str, b: str) -> bool:
    """时刻等值判断：都能解析时按时间差比较（兼容 24:xx），否则按去空白文本比较。"""
    da, db = parse_clock(a), parse_clock(b)
    if da is not None and db is not None:
        return da == db
    return (a or "").strip() == (b or "").strip()


@dataclass
class Correction:
    """一行勘误：站名 + 车次号 + 服务日类型定位门户行，原时刻 → 新时刻。"""

    station: str
    trip_no: str
    service_kind: str   # 已规范化为 工作日/周末/每日
    old_clock: str
    new_clock: str
    line_no: int = 0    # 在勘误表中的行号（从 1 起，不含注释/空行）

    @property
    def label(self) -> str:
        return f"{self.station} · {self.trip_no} · {self.service_kind}"


@dataclass
class PageRow:
    """门户编辑页上的一行。"""

    row_id: str
    station: str
    trip_no: str
    service_kind: str
    clock_field: str    # 时刻输入框字段名，如 clock-7
    value: str


@dataclass
class EditPage:
    """一页编辑表单：全部输入字段（含隐藏令牌/修订号）+ 行数据。"""

    url: str
    action: str
    fields: dict[str, str]
    revision: str
    rows: list[PageRow]
    next_url: str | None


@dataclass
class ItemResult:
    """一条勘误的回填结果。"""

    correction: Correction
    status: str = STATUS_UNKNOWN
    current_clock: str = ""        # 回读到的门户现值
    source_page: str = ""          # 所在编辑页 URL
    notes: list[str] = field(default_factory=list)


@dataclass
class BackfillReport:
    """一次回填作业的完整报告。"""

    items: list[ItemResult]
    errors: list[str] = field(default_factory=list)      # 阻断性错误
    submit_log: list[str] = field(default_factory=list)  # 页面级提交记录
    pages: list[str] = field(default_factory=list)       # 读取到的编辑页 URL
    verified: bool = False                                # 是否完成回读核验


def parse_corrections(text: str) -> tuple[list[Correction], list[str]]:
    """解析勘误表文本，每行：``站名,车次号,服务日类型,原时刻,新时刻``。

    支持中英文逗号；``#`` 开头为注释，空行忽略。返回 ``(勘误列表, 错误列表)``。
    """
    items: list[Correction] = []
    errors: list[str] = []
    seen: dict[tuple[str, str, str], int] = {}

    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in re.split(r"[,，]", line)]
        if len(parts) != 5 or any(part == "" for part in parts):
            errors.append(
                f"第 {line_no} 行格式无效：应为「站名,车次号,服务日类型,原时刻,新时刻」五列。"
            )
            continue
        station, trip_no, kind_text, old_clock, new_clock = parts
        kind = _norm_kind(kind_text)
        if kind is None:
            errors.append(
                f"第 {line_no} 行服务日类型「{kind_text}」无法识别（应为 工作日/周末/每日）。"
            )
            continue
        bad = [
            label
            for label, value in (("原时刻", old_clock), ("新时刻", new_clock))
            if parse_clock(value) is None
        ]
        if bad:
            errors.append(
                f"第 {line_no} 行{'、'.join(bad)}格式无效（HH:MM，支持 24:xx 跨日时刻）。"
            )
            continue
        if _clock_eq(old_clock, new_clock):
            errors.append(f"第 {line_no} 行原时刻与新时刻相同，无需回填。")
            continue
        key = (_norm(station), _norm(trip_no), kind)
        if key in seen:
            errors.append(f"第 {line_no} 行与第 {seen[key]} 行定位相同（站名+车次号+服务日类型），请合并。")
            continue
        seen[key] = line_no
        items.append(
            Correction(
                station=station,
                trip_no=trip_no,
                service_kind=kind,
                old_clock=old_clock,
                new_clock=new_clock,
                line_no=line_no,
            )
        )
    return items, errors


def _parse_edit_page(content: bytes, url: str) -> EditPage | None:
    """从 HTML 中解析编辑表单；找不到含 ``_revision`` 的表单时返回 ``None``。"""
    soup = BeautifulSoup(content, "html.parser")
    form = None
    for candidate in soup.find_all("form"):
        if candidate.find("input", attrs={"name": "_revision"}):
            form = candidate
            break
    if form is None:
        return None

    fields: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")

    rows: list[PageRow] = []
    for tr in form.find_all("tr", attrs={"data-row-id": True}):
        clock_input = tr.find("input", attrs={"name": re.compile(r"^clock-")})
        if clock_input is None:
            continue
        rows.append(
            PageRow(
                row_id=tr["data-row-id"],
                station=tr.get("data-station", ""),
                trip_no=tr.get("data-trip", ""),
                service_kind=tr.get("data-service", ""),
                clock_field=clock_input["name"],
                value=clock_input.get("value", ""),
            )
        )

    next_link = soup.select_one("a.next-page")
    next_url = urljoin(url, next_link["href"]) if next_link and next_link.get("href") else None
    return EditPage(
        url=url,
        action=urljoin(url, form.get("action") or url),
        fields=fields,
        revision=fields.get("_revision", ""),
        rows=rows,
        next_url=next_url,
    )


def _get(session: requests.Session, url: str) -> bytes:
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def _fetch_edit_page(session: requests.Session, url: str) -> EditPage:
    """读取单页编辑表单；失败抛异常，由调用方记录。"""
    content = _get(session, url)
    page = _parse_edit_page(content, url)
    if page is None:
        raise LookupError(f"编辑页（{url}）未找到含 _revision 的编辑表单。")
    return page


def fetch_edit_pages(
    session: requests.Session, portal_url: str
) -> tuple[list[EditPage], list[str]]:
    """从门户 URL 起沿 ``a.next-page`` 读取全部编辑页，返回 ``(页面列表, 错误列表)``。

    错误列表非空表示分页未取全（抓取失败 / 表单缺失 / 循环链接 / 超上限），
    调用方应中止后续提交与核验。
    """
    errors: list[str] = []
    pages: list[EditPage] = []

    try:
        content = _get(session, portal_url)
    except requests.RequestException as exc:
        return [], [f"读取门户首页失败（{portal_url}）：{exc}"]

    page = _parse_edit_page(content, portal_url)
    if page is None:
        # 首页不是编辑页：找编辑入口链接
        soup = BeautifulSoup(content, "html.parser")
        entry = soup.select_one("a.edit-entry")
        href = entry.get("href") if entry else None
        if not href:
            return [], [f"门户首页（{portal_url}）未找到编辑入口链接（a.edit-entry）。"]
        entry_url = urljoin(portal_url, href)
        try:
            page = _fetch_edit_page(session, entry_url)
        except (requests.RequestException, LookupError) as exc:
            return [], [f"读取编辑首页失败：{exc}"]

    seen: set[str] = set()
    while page is not None:
        if page.url in seen:
            errors.append(f"编辑分页出现循环链接（{page.url}），页面未取全。")
            break
        seen.add(page.url)
        pages.append(page)
        if not page.next_url:
            break
        if len(pages) >= MAX_PAGES:
            errors.append(f"编辑分页超过 {MAX_PAGES} 页上限，页面未取全。")
            break
        try:
            page = _fetch_edit_page(session, page.next_url)
        except (requests.RequestException, LookupError) as exc:
            errors.append(f"读取编辑分页失败：{exc}")
            break
    return pages, errors


def _find_row_in_page(page: EditPage, corr: Correction) -> PageRow | None:
    for row in page.rows:
        if (
            _norm(row.station) == _norm(corr.station)
            and _norm(row.trip_no) == _norm(corr.trip_no)
            and _norm_kind(row.service_kind) == corr.service_kind
        ):
            return row
    return None


def _find_row(pages: list[EditPage], corr: Correction) -> tuple[EditPage, PageRow] | None:
    for page in pages:
        row = _find_row_in_page(page, corr)
        if row is not None:
            return page, row
    return None


def _submit_page(
    session: requests.Session,
    first_page: EditPage,
    page_label: str,
    items: list[ItemResult],
    report: BackfillReport,
) -> None:
    """对一页执行「预检 → 整页提交」，页修订冲突时重读并最多重试一次。"""
    page = first_page
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            try:
                page = _fetch_edit_page(session, page.url)
            except (requests.RequestException, LookupError) as exc:
                report.submit_log.append(f"{page_label}：修订冲突后重读失败（{exc}），转入回读核验。")
                return

        # 提交前逐行核对（每次尝试都基于最新读到的页面重新核对）
        changes: dict[str, str] = {}
        for item in items:
            item.notes.clear()
            corr = item.correction
            row = _find_row_in_page(page, corr)
            if row is None:
                item.notes.append("预检：页内找不到匹配行，未改。")
                continue
            if _clock_eq(row.value, corr.new_clock):
                item.notes.append(f"预检：门户现值 {row.value} 已是新时刻，跳过。")
            elif _clock_eq(row.value, corr.old_clock):
                changes[row.clock_field] = corr.new_clock
                item.notes.append(f"预检：现值 {row.value} 仍为原时刻，提交修改。")
            else:
                item.notes.append(
                    f"预检冲突：门户现值 {row.value}，既非原时刻 {corr.old_clock} "
                    f"也非新时刻 {corr.new_clock}，未改。"
                )

        if not changes:
            report.submit_log.append(f"{page_label}：没有待改行，未提交。")
            return

        # 整页提交：无关行按读到的现值原样带回
        payload = dict(page.fields)
        payload.update(changes)
        try:
            resp = session.post(page.action, data=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            report.submit_log.append(
                f"{page_label}：提交后响应中断/异常（{type(exc).__name__}），"
                "不自动重提，转入回读核验。"
            )
            return

        if resp.status_code < 400:
            report.submit_log.append(
                f"{page_label}：已提交 {len(changes)} 行修改（修订号 {page.revision}，HTTP {resp.status_code}）。"
            )
            return
        if resp.status_code == 409 and attempt < MAX_ATTEMPTS:
            report.submit_log.append(
                f"{page_label}：页修订号 {page.revision} 已过期（HTTP 409），重读该页后按同一规则重试。"
            )
            continue
        report.submit_log.append(
            f"{page_label}：提交被拒（HTTP {resp.status_code}），不再重试，转入回读核验。"
        )
        return


def _verify(session: requests.Session, portal_url: str, report: BackfillReport) -> None:
    """回读门户，逐项核验最终状态。"""
    pages, errors = fetch_edit_pages(session, portal_url)
    if errors:
        report.errors.extend(f"回读核验失败：{msg}" for msg in errors)
        for item in report.items:
            item.status = STATUS_UNKNOWN
            item.notes.append("回读取不到门户数据，待人工核实。")
        return

    report.verified = True
    for item in report.items:
        corr = item.correction
        hit = _find_row(pages, corr)
        if hit is None:
            item.status = STATUS_CONFLICT
            item.current_clock = ""
            item.notes.append("回读：门户各页均找不到该行。")
            continue
        page, row = hit
        item.source_page = page.url
        item.current_clock = row.value
        if _clock_eq(row.value, corr.new_clock):
            item.status = STATUS_DONE
        elif _clock_eq(row.value, corr.old_clock):
            item.status = STATUS_UNDONE
        else:
            item.status = STATUS_CONFLICT
        item.notes.append(f"回读：现值 {row.value}。")


def run_backfill(
    portal_url: str,
    corrections: list[Correction],
    session: requests.Session | None = None,
) -> BackfillReport:
    """执行一次回填作业：读取 → 预检 → 提交（含一次重试）→ 回读核验。"""
    report = BackfillReport(items=[ItemResult(correction=c) for c in corrections])
    session = session or requests.Session()

    if not corrections:
        report.errors.append("勘误表为空，没有需要回填的行。")
        return report

    pages, errors = fetch_edit_pages(session, portal_url)
    report.pages = [page.url for page in pages]
    if errors:
        report.errors.extend(errors)
        report.errors.append("编辑分页未取全，已中止提交，请人工核实门户状态。")
        for item in report.items:
            item.status = STATUS_UNKNOWN
        return report

    # 逐条定位所在页与行
    for item in report.items:
        hit = _find_row(pages, item.correction)
        if hit is None:
            item.status = STATUS_CONFLICT
            item.notes.append("门户各页均未找到匹配行（站名+车次号+服务日类型）。")
        else:
            item.source_page = hit[0].url

    # 按页分组提交（只处理有匹配勘误的页）
    for index, page in enumerate(pages, 1):
        page_items = [it for it in report.items if it.source_page == page.url]
        if not page_items:
            continue
        _submit_page(session, page, f"第 {index} 页（{page.url}）", page_items, report)

    _verify(session, portal_url, report)
    return report
