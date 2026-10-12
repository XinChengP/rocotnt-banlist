"""HTML 解析模块。

把官网页面转换成结构化数据，分两步：
1. parse_issue_list   —— 从“新闻公告”列表页解析出所有公告的标题与链接；
2. parse_issue_detail —— 从“更新速递”详情页解析出每周禁赛名单。

设计思路（重要）：
官网不同期的公告文字写法并不完全一致（标题有时叫“系别禁赛宠与参赛系别”，
有时叫“系别排位赛本周参与系别及禁赛宠物更新”），所以解析时不依赖整句标题，
而是靠稳定的关键词片段（“参赛系别”“禁赛宠物”“本周天梯禁赛”）来定位，
以尽量兼容各期格式。
"""

import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from config import SITE_ROOT

# 宠物名/系别名之间可能出现的分隔符：
# 官网混用中文分号、英文分号、中文逗号、英文逗号、顿号以及空白字符
_NAME_SPLIT_RE = re.compile(r"[；;，,、\s]+")

# 匹配“参赛系别”段落，例如：
#   ●本周五参赛系别：火系，冰系，光系。
#   ●下周二参赛系别：普通系，电系，龙系。
# 分组 1 = 周期（本周五 / 下周二），分组 2 = 系别串
_TYPE_PERIOD_RE = re.compile(
    r"[●·\s]*(本周[一二三四五六日天]|下周[一二三四五六日天])\s*参赛系别[:：]\s*(.+?)[。．]"
)

# 匹配系别排位赛的禁赛名单，例如：禁赛宠物：乾镜双凤；煌明火王；……
# 注意这里必须有中文/英文冒号，借此与天梯的“本周禁赛宠物包括……”区分开
_CATEGORY_BAN_RE = re.compile(r"禁赛宠物[:：]\s*(.+?)[。．]\s*$")

# 匹配天梯禁赛名单，例如：本周禁赛宠物包括破晓凯瑞尔，美狄亚，……
_LADDER_BAN_RE = re.compile(r"本周禁赛宠物包括(.+?)[。．]\s*$")

# “本周告示”区块的结束标志：遇到下一个大标题（四、五、……）时停止解析，
# 避免把“四、下周活动预告”里的内容误当成禁赛数据
_SECTION_END_RE = re.compile(r"^[四五]、")

# 从公告网址中提取公告编号，例如 .../202601/980802.shtml -> 980802
_ISSUE_ID_RE = re.compile(r"/(\d+)\.shtml")

# 从公告标题中提取“X月X日”，例如“洛克王国10月9日更新速递” -> (10, 9)。
# 这个日期就是该期公告的“生效周五”，是判断它属于哪个周次的关键。
_TITLE_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _split_names(text: str) -> list[str]:
    """按常见分隔符拆分名称串，并去掉空项。

    参数:
        text: 形如“乾镜双凤；煌明火王；春节”的原始串
    返回:
        拆分后的名称列表，如 ["乾镜双凤", "煌明火王", "春节"]
    """
    return [name for name in _NAME_SPLIT_RE.split(text) if name]


def parse_effective_date(title: str, publish_date: str) -> str | None:
    """从公告标题里解析出“生效日期”，即该期覆盖的周次的周五。

    为什么这样做：
    实测每一期“更新速递”的标题日期，就是该期生效的那个周五
    （例如“洛克王国10月9日更新速递”对应周次 10-09 周五 ~ 10-15 周四），
    因此可以据此判断某一期究竟属于哪个周次。

    参数:
        title: 公告标题，如“洛克王国10月9日更新速递”
        publish_date: 列表页给出的发布日期（YYYY-MM-DD），用于补全年份
    返回:
        生效日期的字符串（YYYY-MM-DD）；标题无日期或发布日期缺失时返回 None。
    """
    matched = _TITLE_DATE_RE.search(title or "")
    if not matched:
        return None

    month, day = int(matched.group(1)), int(matched.group(2))

    # 标题只有月日、没有年份，必须借助发布日期补全。发布日期缺失就无法推断，直接放弃。
    try:
        published = date.fromisoformat(publish_date)
    except (ValueError, TypeError):
        return None

    try:
        effective = date(published.year, month, day)
    except ValueError:
        return None  # 标题里的月日非法（理论上不会发生）

    # 生效日通常紧跟在发布日之后（0~14 天内）。若算出来的日期比发布日早了 45 天以上，
    # 说明是跨年情形（例如 12 月底发布次年 1 月初的那期），此时年份要加一。
    if (published - effective).days > 45:
        try:
            effective = date(published.year + 1, month, day)
        except ValueError:
            return None

    return effective.isoformat()


def parse_issue_list(html: str, keyword: str = "") -> list[dict]:
    """解析“新闻公告”列表页，返回公告条目列表。

    参数:
        html: 列表页的 HTML 文本
        keyword: 标题过滤关键词，仅保留标题包含它的公告（如“更新速递”）
    返回:
        列表，每个元素形如：
        {"title": "洛克王国10月9日更新速递", "url": "https://...", "date": "2026-09-30"}
        顺序与页面一致（官网为倒序，最新公告在最前）。
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    # 列表容器 class 为 newslist，里面每个 <li> 是一条公告
    for li in soup.select("div.newslist li"):
        links = li.find_all("a", href=True)
        if not links:
            continue

        # 每个 <li> 内有两个链接：先是分类标签（如 [公告]），后是公告标题。
        # 因此取最后一个链接作为正文地址与标题。
        title_link = links[-1]
        title = title_link.get_text(strip=True)
        href = title_link.get("href", "")

        # 关键词过滤：只要“更新速递”这类公告
        if keyword and keyword not in title:
            continue

        # 发布日期在 <span class="date"> 中
        date_span = li.find("span", class_="date")
        date = date_span.get_text(strip=True) if date_span else ""

        # 页面给出的是相对路径，需要拼成完整网址
        url = href if href.startswith("http") else SITE_ROOT + href

        results.append({"title": title, "url": url, "date": date})

    return results


def parse_issue_detail(html: str, issue: dict) -> dict:
    """解析“更新速递”详情页，提取每周禁赛名单。

    参数:
        html: 详情页的 HTML 文本
        issue: 由 parse_issue_list 得到的条目，含 title / url / date
    返回:
        结构化字典，字段见函数末尾的 return。
    """
    soup = BeautifulSoup(html, "html.parser")

    # 正文由大量 <p> 组成。这里先取出每个段落的纯文本，再逐段用关键词定位，
    # 比依赖具体的 CSS 样式更稳健（官网只改样式不改文案时不会失效）。
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    paragraphs = [text for text in paragraphs if text]  # 过滤空段落

    category_bans: list[dict] = []  # 系别排位赛禁赛，按“周期”分组
    ladder_bans: list[str] = []     # 天梯禁赛宠物
    in_notice = False               # 是否已进入“本周告示”区块
    current_group: dict | None = None  # 当前正在填充的系别分组

    for text in paragraphs:
        # 第一步：先找到“本周告示”所在段落，之后的内容才是我们关心的
        if not in_notice:
            if "本周告示" in text:
                in_notice = True
            continue

        # 第二步：遇到下一个大标题（四、五、…）说明告示区结束，立即停止
        if _SECTION_END_RE.match(text):
            break

        # 第三步：解析“参赛系别”段落，开启一个新的周期分组
        matched = _TYPE_PERIOD_RE.search(text)
        if matched:
            current_group = {
                "period": matched.group(1),          # 如“本周五”“下周二”
                "types": _split_names(matched.group(2)),  # 参赛系别列表
                "pets": [],                          # 稍后填入该周期的禁赛宠物
            }
            category_bans.append(current_group)
            continue

        # 第四步：解析天梯禁赛段落（注意要放在系别“禁赛宠物：”判断之前，
        # 因为两者都含“禁赛宠物”字样，靠“包括”和冒号区分）
        matched = _LADDER_BAN_RE.search(text)
        if matched:
            ladder_bans = _split_names(matched.group(1))
            current_group = None  # 天梯与系别无关，重置分组避免误挂
            continue

        # 第五步：解析系别禁赛名单，挂到最近一次出现的周期分组下
        matched = _CATEGORY_BAN_RE.search(text)
        if matched and current_group is not None:
            current_group["pets"] = _split_names(matched.group(1))
            continue

    # 从网址中提取公告编号，作为唯一主键
    id_matched = _ISSUE_ID_RE.search(issue["url"])
    issue_id = id_matched.group(1) if id_matched else issue["url"]

    # 解析该期的“生效周五”，并换算出它覆盖的周次区间 [周五, 下周四]。
    # 这两个字段供上层挑选“当前周次对应的一期”，也方便调用方核对数据属于哪一周。
    effective_date = parse_effective_date(issue["title"], issue["date"])
    if effective_date:
        week_start = effective_date  # 周次起点：生效的那个周五
        week_end = (date.fromisoformat(effective_date) + timedelta(days=6)).isoformat()  # 周次终点：下周四
    else:
        # 标题里没有日期（官网若改版可能导致），此时无法归类周次，相关字段留空
        week_start = None
        week_end = None

    return {
        "issue_id": issue_id,
        "title": issue["title"],
        "url": issue["url"],
        "publish_date": issue["date"],      # 列表页给出的发布日期
        "effective_date": effective_date,   # 生效日期（该期的周五）
        "week_start": week_start,           # 周次起点（周五）
        "week_end": week_end,               # 周次终点（下周四）
        "category_bans": category_bans,     # 系别排位赛禁赛（含周期、系别、宠物）
        "ladder_bans": ladder_bans,         # 天梯禁赛宠物
    }
