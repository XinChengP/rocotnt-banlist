"""项目全局配置。

把网址、请求头、数据库路径等常量集中在一个文件里，
后续官网地址变化或需要调整抓取策略时，只需改这里，不用翻遍全部代码。
"""

from pathlib import Path

# 项目根目录：即当前文件（config.py）所在目录
BASE_DIR = Path(__file__).resolve().parent

# 数据目录：用于存放 SQLite 数据库文件，程序启动时会自动创建
DATA_DIR = BASE_DIR / "data"

# SQLite 数据库文件的完整路径
DB_PATH = DATA_DIR / "roco_ban.db"

# 官网“新闻公告”列表页。
# 实测该页面可正常访问（HTTP 200），且包含最新一期公告的标题与链接，
# 因此把它作为“自动发现最新一期更新速递”的入口。
# 注意：不能直接访问 m682 目录（会返回 403），必须从这个列表页取链接。
LIST_URL = "http://roco.qq.com/webplat/info/news_version3/397/838/839/840/m681/list_1.shtml"

# 站点根地址：列表页里给的是相对路径（/webplat/...），需要与它拼接成完整网址
SITE_ROOT = "https://roco.qq.com"

# 目标公告的标题关键词：只有标题包含该词的公告才是我们要的“更新速递”
ISSUE_TITLE_KEYWORD = "更新速递"

# 请求头。
# 实测：不带 User-Agent 访问官网部分页面会返回 403，
# 因此这里伪装成常见浏览器；Referer 指向官网首页，进一步降低被识别为爬虫的概率。
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://roco.qq.com/",
}

# 网络请求超时时间（秒），避免网络异常时程序被长时间卡死
REQUEST_TIMEOUT = 25

# 页面编码。
# 官网页面在 <meta> 里声明的是 gb2312，但正文中含有 gb2312 字符集之外的生僻字
# （例如宠物名“青钟翾狐”“螣岚”），用 gb2312 解码会出现乱码（显示为“□”或问号）。
# 实际编码是 gb18030（gb2312 的超集），因此统一用 gb18030 解码。
PAGE_ENCODING = "gb18030"

# 每次采集时，从列表页最多抓取多少期“更新速递”。
# 为什么不止抓一期：官网有时一次发布多期（例如 09-23 那天就同时发布了 9/25 与 10/2 两期），
# 若只抓最新一期，会导致“当前周次”真正需要的那期根本没入库，跨周时无数据可用；
# 抓最近若干期，才能保证任何时候都能从库里挑出“当前周次”对应的那一期。
RECENT_ISSUE_LIMIT = 5
