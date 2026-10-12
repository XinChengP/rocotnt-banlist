"""采集编排模块。

职责：把“发请求 → 解析 → 存库”这条链路串起来，对外暴露三个入口函数：
- discover_recent_issues()  找出最近若干期“更新速递”公告
- update_once()             把这些期全部抓下来并写入数据库
- select_current_issue()    从若干期里挑出“当前周次（周五~下周四）”对应的那一期

为什么单独拆出这一层：
parser.py 只负责“把 HTML 变数据”，storage.py 只负责“把数据落库”，
它们都不关心“数据从哪来、按什么顺序走”。把编排逻辑集中在这里，
以后要改成定时任务、并发抓多期或换数据源，只动这个文件即可。
"""

from datetime import date
from typing import Optional

import requests

from config import (
    LIST_URL,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    PAGE_ENCODING,
    ISSUE_TITLE_KEYWORD,
    RECENT_ISSUE_LIMIT,
)
from parser import parse_issue_list, parse_issue_detail, parse_effective_date


def fetch_html(url: str) -> str:
    """下载网页并返回正确解码后的 HTML 文本。

    参数:
        url: 目标页面地址
    返回:
        解码后的 HTML 字符串
    异常:
        requests.RequestException —— 网络错误、超时、HTTP 错误码等统一抛出，
        由上层决定如何处理（例如接口返回 502）。
    """
    # 带上伪装成浏览器的请求头，避免被官网识别为爬虫而返回 403
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()  # 4xx/5xx 直接抛异常，不要拿错误页去解析

    # 关键：官网在 <meta> 里写的是 gb2312，但正文含生僻字，实际须按 gb18030 解码。
    # 这里手动指定编码，而不是让 requests 自己猜，避免出现乱码。
    resp.encoding = PAGE_ENCODING
    return resp.text


def discover_recent_issues(limit: int = RECENT_ISSUE_LIMIT) -> list[dict]:
    """从列表页找出最近若干期“更新速递”公告。

    为什么不只取最新一期：官网有时一次发布多期（例如 09-23 当天同时发布了
    9/25 与 10/2 两期），若只取最新一期，“当前周次”真正需要的那期就会漏抓，
    导致跨周时库里没有对应数据可用。

    参数:
        limit: 最多返回多少期
    返回:
        列表，每项形如 {"title": ..., "url": ..., "date": ...}；
        顺序与页面一致（官网为倒序，最新在前）；找不到时返回空列表。
    """
    html = fetch_html(LIST_URL)
    # 按关键词过滤，只保留“更新速递”这类公告
    issues = parse_issue_list(html, keyword=ISSUE_TITLE_KEYWORD)
    return issues[:limit]


def scrape_issue(issue: dict) -> dict:
    """抓取并解析指定一期公告。

    参数:
        issue: discover_recent_issues 返回的条目（含 title / url / date）
    返回:
        parser.parse_issue_detail 产出的结构化字典
    """
    html = fetch_html(issue["url"])
    return parse_issue_detail(html, issue)


def _issue_effective_date(item: dict) -> Optional[str]:
    """取得某一期的“生效日期”（该期的周五），取不到则返回 None。

    为什么要单独抽一个函数：选期逻辑既可能面对“新数据”（已带 effective_date 字段，
    是解析时就算好存进库的），也可能面对“旧数据”（线上历史 KV/数据库里没这个字段）。
    对旧数据现场用标题 + 发布日期重新推算一次，就能避免为了加字段而迁移历史数据。

    参数:
        item: 单期数据字典，可能含 effective_date，也可能只有 title / publish_date
    返回:
        ISO 日期字符串（如 "2025-09-25"），无法解析时为 None
    """
    # 优先用解析时已存好的字段；缺失时退回“现场推算”
    effective = item.get("effective_date")
    if effective:
        return effective
    # 旧数据没有 effective_date，但有标题和发布日期，可以现场重算
    if item.get("title") and item.get("publish_date"):
        return parse_effective_date(item["title"], item["publish_date"])
    return None


def select_current_issue(items: list[dict], today: Optional[str] = None) -> Optional[dict]:
    """从若干期公告中挑出“当前周次”对应的那一期。

    选期规则（用户确认）：返回“已生效的最近一期”，也就是在生效日期 <= 今天的所有期里，
    取生效日期最大的那一期；如果全部期都还没生效（生效日期都在未来），
    则兜底返回生效日期最小的那一期（即最近将要生效的一期），避免完全没数据输出。

    为什么不能简单取“最新发布的一期”：官网有时一次发布多期（例如 09-23 当天同时发了
    9/25 与 10/2 两期），此时“最新发布”的 10/2 期其实还没生效，而 09-23 当天真正在用的
    是 9/18 期。只有按“生效日期 <= 今天”来筛，才能保证输出与当前周次一致。

    参数:
        items: 若干期数据（每期含 effective_date，或含 title/publish_date 供现场推算）
        today: 基准日期（ISO 字符串），默认取服务器当天；主要用于测试时传入固定日期
    返回:
        命中的那一期字典；items 为空或全部无法解析出生效日期时返回 None
    """
    if not items:
        return None

    # 基准日期：默认用服务器当天。这里只用日期、不用时间，避免时区差导致误判。
    base = today or date.today().isoformat()

    # 先给每一期算出生效日期；算不出来的（标题格式异常等）直接丢弃，不参与选期
    dated: list[tuple[str, dict]] = []
    for item in items:
        effective = _issue_effective_date(item)
        if effective:
            dated.append((effective, item))

    if not dated:
        # 一期都算不出发效日期，无法判断周次，交由上层按“无数据”处理
        return None

    # 已生效的期：生效日期 <= 基准日期。“字符串比较”对 ISO 日期是安全的（格式固定、可直接字典序比较）
    effective_pool = [pair for pair in dated if pair[0] <= base]
    if effective_pool:
        # 在已生效的期里取生效日期最大的那一期，就是“当前周次”对应的那期
        return max(effective_pool, key=lambda pair: pair[0])[1]

    # 兜底：全部期都还没生效，取生效日期最小的一期（最近将要生效的一期）
    return min(dated, key=lambda pair: pair[0])[1]


def update_once() -> dict:
    """执行一次完整的采集：抓最近若干期并全部写入数据库，然后返回“当前周次”那一期。

    为什么要抓多期而不是只抓一期：官网有时一次发布多期（例如 09-23 当天同时发了
    9/25 与 10/2 两期）。如果只抓列表第一条，一来可能抓到“还没生效”的那期，
    二来会漏掉当期真正在用的那一期，导致跨周时库里查不到对应数据。
    因此这里把最近 RECENT_ISSUE_LIMIT 期都抓下来入库，接口再按“当前周次”现取。

    返回:
        {"status": ..., "message": ..., "data": <结构化数据或 None>}
        - status = "ok"         至少成功入库一期，data 为“当前周次”那一期
        - status = "not_found"  列表页没找到任何“更新速递”公告
        - status = "parse_empty" 找到公告但全部解析为空（文案格式疑似变化）

    说明：
    - 返回字典而不是直接抛异常，是为了让 API 的刷新接口能把结果原样告诉调用方；
    - 但“网络异常”这类真正的错误仍然会向上抛，由调用方转成 HTTP 错误码。
    """
    # 延迟导入 storage：避免模块导入时就创建数据目录/连接数据库，
    # 也让本模块可以单独做“只抓不存”的调试。
    import storage

    issues = discover_recent_issues()
    if not issues:
        # 列表页没找到“更新速递”，通常意味着官网改版或关键词变了，需要人工介入
        return {"status": "not_found", "message": "列表页未发现含“更新速递”的公告", "data": None}

    saved: list[dict] = []  # 记录本次成功入库的所有期，供随后选“当前周次”用
    for issue in issues:
        data = scrape_issue(issue)

        # 健全性检查：如果系别禁赛与天梯禁赛都为空，说明解析规则可能已失效。
        # 这时跳过这一期就好，不要写库——避免用空数据覆盖掉历史里正常的同期数据。
        if not data["category_bans"] and not data["ladder_bans"]:
            continue

        storage.save_issue(data)
        saved.append(data)

    if not saved:
        # 抓到了公告，但没有任何一期能解析出内容，多半是文案模板变了，需要人工检查
        return {
            "status": "parse_empty",
            "message": "找到公告但全部解析为空，可能是文案格式变化，请检查解析规则",
            "data": None,
        }

    # 从本次入库的期里挑出“当前周次（周五~下周四）”对应的那一期作为返回结果
    current = select_current_issue(saved)
    return {
        "status": "ok",
        "message": f"抓取并入库 {len(saved)} 期成功",
        "data": current,
    }
