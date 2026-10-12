"""FastAPI 应用入口。

对外提供 4 组接口：
- GET  /health                    健康检查
- GET  /api/ban/current           当前周次（周五~下周四）对应的禁赛数据
- GET  /api/ban/history           历史期数概要列表
- GET  /api/ban/issues/{id}       指定某一期详情
- POST /api/ban/refresh           手动触发一次抓取并入库

启动方式：
    python main.py
默认监听 127.0.0.1:8000，浏览器打开 http://127.0.0.1:8000/docs 可看到自动生成的接口文档。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
import requests

import storage
import scraper


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    启动时初始化数据库表；关闭时无需额外清理（SQLite 连接是即用即关的）。
    用 lifespan 而不是 @app.on_event("startup")，因为后者在新版 FastAPI 已废弃。
    """
    storage.init_db()
    yield


app = FastAPI(
    title="洛克王国每周禁赛查询 API",
    description="自动抓取官网“更新速递”公告中的系别排位赛禁赛宠与天梯禁赛宠。",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", summary="健康检查")
def health() -> dict:
    """用于探活，返回固定的 ok 状态，不查数据库。"""
    return {"status": "ok"}


@app.get("/api/ban/current", summary="当前周次禁赛数据")
def get_current() -> dict:
    """返回“当前周次（周五~下周四）”对应的那一期完整数据。

    为什么不直接返回发布时间最新的一期：官网有时一次发布多期（例如 09-23 当天
    同时发了 9/25 与 10/2 两期），此时最新的 10/2 期其实还没生效。
    所以这里取出库里全部期，再按“生效日期 <= 今天、取生效日期最大的一期”来选，
    保证输出始终对应当前所在周次。

    若数据库为空（还没抓过），返回 404，提示先调用刷新接口。
    """
    items = storage.get_all_issues()
    data = scraper.select_current_issue(items)
    if data is None:
        raise HTTPException(status_code=404, detail="暂无数据，请先调用 POST /api/ban/refresh 抓取")
    return data


@app.get("/api/ban/history", summary="历史期数概要")
def get_history(limit: int = Query(10, ge=1, le=52, description="返回的最大期数")) -> dict:
    """返回历史期数的概要列表（不含完整名单，减小响应体积）。

    参数 limit 用 Query 做了范围约束（1~52），即约一年内的期数上限。
    """
    return {"count": limit, "items": storage.get_history(limit)}


@app.get("/api/ban/issues/{issue_id}", summary="指定期数详情")
def get_issue(issue_id: str) -> dict:
    """按公告编号查询某一期的完整数据。查不到返回 404。"""
    data = storage.get_issue_by_id(issue_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"未找到编号为 {issue_id} 的公告")
    return data


@app.post("/api/ban/refresh", summary="手动触发抓取")
def refresh() -> dict:
    """立即执行一次采集：发现最近若干期公告 → 逐期抓取解析 → 入库。

    返回 scraper.update_once 的结果，status 取值：
    - ok          抓取并入库成功（data 为当前周次那一期）
    - not_found   列表页未发现目标公告
    - parse_empty 解析结果为空（疑似格式变化，未入库）
    网络异常则返回 502。
    """
    try:
        result = scraper.update_once()
    except requests.RequestException as exc:
        # 把网络/HTTP 层错误转成 502，让调用方明确知道是“上游官网出问题”
        raise HTTPException(status_code=502, detail=f"抓取官网失败：{exc}") from exc
    return result


if __name__ == "__main__":
    # 直接以脚本方式运行本文件时启动开发服务器。
    # host 用 127.0.0.1 仅本机可访问；若需局域网访问可改为 "0.0.0.0"。
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
