"""数据存储模块（基于 SQLite）。

设计说明：
- 只使用一张 issues 表，把每期公告的完整结构化结果（category_bans / ladder_bans）
  以 JSON 字符串整体存进 payload 字段。
- 这样做的原因：每周数据量极小、结构就是这一份 JSON，用单表最省事；
  不必为此拆出多张关联表（避免过度设计），查询时反序列化即可。
- 主键用公告编号 issue_id，天然实现“同一期重复抓取不会产生重复记录”。
"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta

from config import DB_PATH, DATA_DIR

# 北京时间时区（UTC+8），用于记录抓取时间，便于阅读
BEIJING_TZ = timezone(timedelta(hours=8))


def _connect() -> sqlite3.Connection:
    """创建数据库连接。

    每次操作都新建连接，避免多线程（FastAPI 默认线程池）下共享连接出问题。
    这是简单可靠的做法，本项目数据量极小，性能无压力。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)  # 确保数据目录存在
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 让查询结果可以按列名访问，代码更易读
    return conn


def init_db() -> None:
    """初始化数据库表结构。

    使用 IF NOT EXISTS，重复调用不会报错，可安全地在每次启动时执行。
    """
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS issues (
                issue_id     TEXT PRIMARY KEY,   -- 公告编号，唯一标识一期数据
                title        TEXT NOT NULL,      -- 公告标题
                publish_date TEXT,               -- 公告发布日期（YYYY-MM-DD）
                url          TEXT,               -- 公告原始地址
                fetched_at   TEXT,               -- 本程序抓取入库的时间
                payload      TEXT NOT NULL       -- 完整结构化数据（JSON 字符串）
            )
            """
        )
        conn.commit()


def save_issue(payload: dict) -> None:
    """保存（或覆盖）一期公告数据。

    参数:
        payload: parser.parse_issue_detail 返回的结构化字典
    """
    fetched_at = datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
    with _connect() as conn:
        # REPLACE 语义：同一 issue_id 再次抓取时直接覆盖，保证数据是最新的
        conn.execute(
            """
            INSERT OR REPLACE INTO issues
                (issue_id, title, publish_date, url, fetched_at, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["issue_id"],
                payload["title"],
                payload.get("publish_date", ""),
                payload.get("url", ""),
                fetched_at,
                json.dumps(payload, ensure_ascii=False),  # 保证中文不被转义成 \uXXXX
            ),
        )
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    """把数据库行转换成对外返回的字典格式。"""
    data = json.loads(row["payload"])
    data["fetched_at"] = row["fetched_at"]
    return data


def get_all_issues() -> list[dict]:
    """获取数据库里已入库的全部期数据（含完整名单）。

    为什么需要“取全部”而不是“取最新一期”：接口要按“当前周次（周五~下周四）”
    挑选对应那一期，而当前周次对应的那期不一定是发布时间最新的一期
    （官网有时一次发多期，最新的那期可能还没生效）。
    因此必须把所有已入库的期都取出来，再交给 scraper.select_current_issue() 去选。

    返回:
        列表，按发布日期倒序（最新在前）；数据库为空时返回空列表。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM issues ORDER BY publish_date DESC, issue_id DESC"
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_issue_by_id(issue_id: str) -> dict | None:
    """按公告编号查询某一期详情。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_history(limit: int = 12) -> list[dict]:
    """获取历史期数（简要信息，不含完整名单，减小响应体积）。

    参数:
        limit: 返回的最大条数
    返回:
        列表，每项含 issue_id / title / publish_date / url 等概要字段。
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT issue_id, title, publish_date, url, fetched_at
            FROM issues
            ORDER BY publish_date DESC, issue_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
