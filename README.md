# 洛克王国每周禁赛查询 API

自动抓取洛克王国官网「更新速递」公告中每周的**系别排位赛禁赛宠物**与**天梯禁赛宠物**，
对外提供 REST API 查询。

## 功能特性

- 自动发现并解析官网「更新速递」公告，提取禁赛名单
- 按「当前周次（周五 ~ 下周四）」返回对应那一期数据
  - 官网有时一次发布多期（例如同一天发了下周与下下周两期），
    本项目的选期逻辑保证返回的始终是**当前生效**的那一期，而不是单纯「最新发布」的那期
- 支持本地运行（Python + FastAPI + SQLite）与线上部署（Cloudflare Workers + KV + 定时任务）
- 每周四北京时间 0 点自动抓取（周三全天已过完，官网最新一期必定已发布）

## 两种运行方式

### 方式一：本地运行（Python）

```bash
# 安装依赖
python -m pip install -r requirements.txt

# 初始化并抓取一次数据（命令行）
python update_now.py

# 启动 API 服务
python main.py
```

启动后访问 `http://127.0.0.1:8000/docs` 可查看自动生成的接口文档。

### 方式二：线上部署（Cloudflare Workers）

见下方「部署到 Cloudflare Workers」。

## API 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/ban/current` | 当前周次（周五~下周四）对应的完整禁赛数据 |
| GET | `/api/ban/history?limit=10` | 历史期数概要列表（limit 范围 1~52） |
| GET | `/api/ban/issues/{issue_id}` | 指定某一期的完整数据 |
| POST | `/api/ban/refresh` | 手动触发一次抓取并入库 |

## 目录结构

```
.
├── config.py            # 全局配置（网址、请求头、编码、采集期数等）
├── parser.py            # HTML 解析：列表页 + 详情页 → 结构化数据
├── scraper.py           # 采集编排：发现多期 → 逐期抓取 → 选当前周次
├── storage.py           # 数据存储（SQLite）
├── main.py              # FastAPI 应用入口
├── update_now.py        # 命令行采集入口（可配 Windows 任务计划程序）
├── requirements.txt     # Python 依赖清单
└── cf-worker/           # Cloudflare Workers 版本
    ├── src/index.js     # Worker 主逻辑（与 Python 版一一对应）
    └── wrangler.toml    # 部署配置（含定时任务、KV、自定义域名）
```

## 部署到 Cloudflare Workers

1. 安装并登录 wrangler：

   ```bash
   npm install -g wrangler
   npx wrangler login
   ```

2. 创建 KV 命名空间，把返回的 id 填进 `cf-worker/wrangler.toml`：

   ```bash
   npx wrangler kv namespace create ROCO_BAN
   ```

3. 按需修改 `cf-worker/wrangler.toml` 里的 `[[routes]]`（自定义域名），
   不需要自定义域名可整段删除。

4. 部署：

   ```bash
   cd cf-worker
   npx wrangler deploy
   ```

5. 部署后调用一次 `POST /api/ban/refresh` 初始化数据即可。

## 数据说明

- 每期公告标题里的「X月X日」就是该期的**生效周五**，一期公告对应一个周次（周五 ~ 下周四）
- API 返回的每期数据包含 `effective_date`（生效日期）、`week_start` / `week_end`（周次区间），
  便于调用方核对数据属于哪一周

## 免责声明

本项目仅供学习交流使用，数据来源于洛克王国官网公开公告，版权归原网站所有。
