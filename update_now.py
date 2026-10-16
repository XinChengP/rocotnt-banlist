"""命令行采集入口。

用途：不启动 Web 服务，单独跑一次抓取并入库。
适合两类场景：
1. 首次初始化数据：运行 `python update_now.py` 先把最近几期抓进数据库；
2. 定时任务：Windows「任务计划程序」里配置每天 0 点（北京时间）执行本脚本即可。
   为什么定在每天 0 点：官网公告更新时间只能确定在“周三”，具体几点不固定；
   每天 0 点时前一自然日已过完，既不会漏掉当期，某次失败也能在次日自动补上。

运行：
    python update_now.py
"""

import sys

import storage
import scraper


def main() -> int:
    """执行一次采集，并把结果打印到控制台。

    返回:
        进程退出码：0 表示正常（含“未发现公告”这类业务性提示），
        2 表示发生网络异常等技术性错误，便于任务计划程序判断是否失败。
    """
    storage.init_db()  # 保证表存在

    try:
        result = scraper.update_once()
    except Exception as exc:  # 捕获网络等异常，转为可读提示与退出码
        print(f"[失败] 抓取过程出错：{exc}")
        return 2

    status = result["status"]
    print(f"[状态] {status} —— {result['message']}")

    data = result.get("data")
    if status == "ok" and data:
        # 打印摘要，方便人工快速核对抓取是否正常
        print(f"        公告：{data['title']}（{data['publish_date']}）")
        print(f"        系别禁赛分组数：{len(data['category_bans'])}")
        print(f"        天梯禁赛宠物数：{len(data['ladder_bans'])}")
        for group in data["category_bans"]:
            types = "、".join(group["types"])
            pets = "、".join(group["pets"]) or "（无）"
            print(f"          - {group['period']} 参赛系别：{types}")
            print(f"            禁赛宠物：{pets}")
        if data["ladder_bans"]:
            print(f"        天梯禁赛：{'、'.join(data['ladder_bans'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
