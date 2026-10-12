/**
 * 洛克王国每周禁赛 API —— Cloudflare Workers 版本
 *
 * 与 Python 版本（项目根目录的 parser.py / scraper.py）逻辑一一对应，
 * 区别只是把「requests + BeautifulSoup」换成了「fetch + 正则 / TextDecoder」，
 * 并新增了 KV 存储与定时任务。
 *
 * 运行方式：
 * - 定时：由 wrangler.toml 里的 Cron Trigger 每周三自动调用 scheduled()
 * - 接口：外部 HTTP 请求进入 fetch()，按路径返回 KV 里的数据
 */

// ============================== 常量配置 ==============================

// 官网“新闻公告”列表页。实测可正常访问，且包含最新一期公告的链接。
// 注意：不能直接访问 m682 目录（会返回 403），必须从这个列表页取链接。
const LIST_URL =
  "http://roco.qq.com/webplat/info/news_version3/397/838/839/840/m681/list_1.shtml";

// 站点根地址：列表页给的是相对路径，需要和它拼成完整网址
const SITE_ROOT = "https://roco.qq.com";

// 只抓标题包含该关键词的公告
const ISSUE_TITLE_KEYWORD = "更新速递";

// 每次采集从列表页最多抓取多少期。
// 为什么不止抓一期：官网有时一次发布多期（例如 09-23 当天同时发了 9/25 与 10/2 两期），
// 只抓最新一期会漏掉“当前周次”真正需要的那期。抓最近若干期，才能保证任何时候都能选出生效中的那期。
const RECENT_ISSUE_LIMIT = 5;


// 官网 meta 声称 gb2312，但含生僻字，实际须按 gb18030 解码（gb2312 的超集）
const PAGE_ENCODING = "gb18030";

// 请求头：不带 User-Agent 会被官网返回 403，故伪装成浏览器并带上 Referer
const REQUEST_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  Referer: "https://roco.qq.com/",
};

// ============================== 解析用正则 ==============================
// 以下正则与 Python 版 parser.py 完全一致，逐条注释说明用途。
// 注意：这些都用 search / test，不带 g 标志，避免 lastIndex 状态问题。

// 名称之间的分隔符：官网混用中文/英文分号、中文/英文逗号、顿号、空白
const NAME_SPLIT_RE = /[；;，,、\s]+/;

// “参赛系别”段落，如：●本周五参赛系别：火系，冰系，光系。
// 分组 1 = 周期（本周五/下周二），分组 2 = 系别串
const TYPE_PERIOD_RE =
  /[●·\s]*(本周[一二三四五六日天]|下周[一二三四五六日天])\s*参赛系别[:：]\s*(.+?)[。．]/;

// 系别排位赛禁赛名单，如：禁赛宠物：乾镜双凤；煌明火王；……
// 必须有冒号，借此与天梯的“本周禁赛宠物包括……”区分
const CATEGORY_BAN_RE = /禁赛宠物[:：]\s*(.+?)[。．]\s*$/;

// 天梯禁赛名单，如：本周禁赛宠物包括破晓凯瑞尔，美狄亚，……
const LADDER_BAN_RE = /本周禁赛宠物包括(.+?)[。．]\s*$/;

// “本周告示”区块的结束标志：遇到下一个大标题（四、五、……）即停止
const SECTION_END_RE = /^[四五]、/;

// 从公告网址中提取公告编号，如 .../202601/980802.shtml -> 980802
const ISSUE_ID_RE = /\/(\d+)\.shtml/;

// 从公告标题中提取“X月X日”，如“洛克王国10月9日更新速递” -> (10, 9)。
// 这个日期就是该期的“生效周五”，是判断它属于哪个周次的关键。
const TITLE_DATE_RE = /(\d{1,2})\s*月\s*(\d{1,2})\s*日/;

// ============================== 基础工具函数 ==============================

/**
 * 解码 HTML 实体（&nbsp; &amp; 等）。
 * 官网正文里会出现 &nbsp;，不解码会混进宠物名。
 * 注意 &amp; 必须放最后替换，否则 &amp;lt; 会被二次解码成 &lt;。
 */
function decodeEntities(text) {
  return text
    .replace(/&nbsp;/gi, " ")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
    .replace(/&#x([0-9a-fA-F]+);/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
    .replace(/&amp;/gi, "&");
}

/**
 * 把一段 HTML 片段转成纯文本。
 * 对应 Python 里 BeautifulSoup 的 get_text(" ", strip=True)：
 * 先把所有标签替换成空格（模拟标签边界分隔符），再解码实体，最后压缩空白。
 */
function htmlToText(html) {
  const withoutTags = html.replace(/<[^>]+>/g, " ");
  return decodeEntities(withoutTags).replace(/\s+/g, " ").trim();
}

/** 按分隔符拆分名称串，并去掉空项 */
function splitNames(text) {
  return text.split(NAME_SPLIT_RE).filter((name) => name !== "");
}

/** 把数字补成两位字符串，如 9 -> "09"（用于拼日期） */
function pad2(n) {
  return String(n).padStart(2, "0");
}

/**
 * 给一个 ISO 日期（YYYY-MM-DD）加上若干天，返回新的 ISO 日期字符串。
 * 全部用 UTC 计算，避免服务器本地时区参与运算导致偏移一天。
 */
function addDays(isoDate, days) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!m) return null;
  const base = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const d = new Date(base + days * 86400000);
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
}

/**
 * 取“北京时间当天”的 ISO 日期（YYYY-MM-DD）。
 * Worker 运行在 UTC 环境，直接取本地日期会在北京时间凌晨时段差一天；
 * 这里在时间戳上加 8 小时再取日期，换算成北京时间的“今天”。
 */
function beijingTodayIso() {
  const beijing = new Date(Date.now() + 8 * 3600 * 1000);
  return beijing.toISOString().slice(0, 10);
}

/** 统一构造 JSON 响应（带跨域头，方便前端直接调用） */
function jsonResponse(obj, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...extraHeaders,
    },
  });
}

// ============================== 网络请求 ==============================

/**
 * 下载网页并返回正确解码后的 HTML 文本。
 * 关键点：用 TextDecoder("gb18030") 手动解码，与 Python 版 resp.encoding 行为一致。
 */
async function fetchHtml(url) {
  const resp = await fetch(url, { headers: REQUEST_HEADERS });
  if (!resp.ok) {
    throw new Error(`请求失败 HTTP ${resp.status}：${url}`);
  }
  // 先拿字节，再按 gb18030 解码，避免乱码
  const buffer = await resp.arrayBuffer();
  return new TextDecoder(PAGE_ENCODING).decode(buffer);
}

// ============================== 页面解析 ==============================

/**
 * 解析“新闻公告”列表页，返回公告条目列表（按页面顺序，最新在前）。
 * 对应 Python 版 parser.parse_issue_list。
 *
 * @param {string} html 列表页 HTML
 * @param {string} keyword 标题过滤关键词
 */
function parseIssueList(html, keyword) {
  const results = [];
  // 逐个 <li> 处理。正则放在函数内部定义，避免全局正则的 lastIndex 残留问题。
  const listItemRe = /<li[^>]*>([\s\S]*?)<\/li>/gi;

  for (const liMatch of html.matchAll(listItemRe)) {
    const liHtml = liMatch[1];

    // 每个 <li> 内有多个链接：先是分类标签（如 [公告]），后是标题链接。
    // 因此取“最后一个 .shtml 链接”作为正文地址与标题，与 Python 版一致。
    const linkRe = /<a[^>]+href="([^"]+\.shtml)"[^>]*>([\s\S]*?)<\/a>/gi;
    let lastLink = null;
    for (const lm of liHtml.matchAll(linkRe)) {
      lastLink = lm;
    }
    if (!lastLink) continue;

    const title = htmlToText(lastLink[2]);
    if (keyword && !title.includes(keyword)) continue;

    // 发布日期在 <span class="date"> 中
    const dateMatch = liHtml.match(/<span\s+class="date">([\s\S]*?)<\/span>/i);
    const date = dateMatch ? htmlToText(dateMatch[1]) : "";

    const href = lastLink[1];
    const url = href.startsWith("http") ? href : SITE_ROOT + href;

    results.push({ title, url, date });
  }

  return results;
}

/**
 * 从公告标题里解析“生效日期”，即该期覆盖的周次的周五。
 * 对应 Python 版 parser.parse_effective_date。
 *
 * 为什么这样做：实测每一期“更新速递”的标题日期，就是该期生效的那个周五
 * （例如“洛克王国10月9日更新速递”对应周次 10-09 周五 ~ 10-15 周四），
 * 据此即可判断某一期究竟属于哪个周次。
 *
 * @param {string} title 公告标题，如“洛克王国10月9日更新速递”
 * @param {string} publishDate 列表页给出的发布日期（YYYY-MM-DD），用于补全年份
 * @returns {string|null} 生效日期（YYYY-MM-DD）；标题无日期或发布日期缺失时为 null
 */
function parseEffectiveDate(title, publishDate) {
  const m = TITLE_DATE_RE.exec(title || "");
  if (!m) return null;
  const month = Number(m[1]);
  const day = Number(m[2]);

  // 标题只有月日、没有年份，必须借助发布日期补全；发布日期缺失就无法推断，直接放弃
  const pm = /^(\d{4})-(\d{2})-(\d{2})/.exec(publishDate || "");
  if (!pm) return null;
  const pubYear = Number(pm[1]);
  const pubMonth = Number(pm[2]);
  const pubDay = Number(pm[3]);

  // 生效日通常紧跟在发布日之后（0~14 天内）。若算出来比发布日早了 45 天以上，
  // 说明是跨年情形（例如 12 月底发布次年 1 月初那期），此时年份要加一。
  const pubUtc = Date.UTC(pubYear, pubMonth - 1, pubDay);
  const effUtcSameYear = Date.UTC(pubYear, month - 1, day);
  const year = (pubUtc - effUtcSameYear) / 86400000 > 45 ? pubYear + 1 : pubYear;

  return `${year}-${pad2(month)}-${pad2(day)}`;
}

/**
 * 解析“更新速递”详情页，提取每周禁赛名单。
 * 对应 Python 版 parser.parse_issue_detail，逐段用关键词定位。
 *
 * @param {string} html 详情页 HTML
 * @param {object} issue 由 parseIssueList 得到的条目（含 title / url / date）
 */
function parseIssueDetail(html, issue) {
  // 正文由大量 <p> 组成，先取出每段的纯文本（比依赖 CSS 样式更稳健）
  const paragraphs = [];
  const pTagRe = /<p\b[^>]*>([\s\S]*?)<\/p>/gi;
  for (const pm of html.matchAll(pTagRe)) {
    const text = htmlToText(pm[1]);
    if (text) paragraphs.push(text);
  }

  const categoryBans = []; // 系别排位赛禁赛，按“周期”分组
  let ladderBans = []; // 天梯禁赛宠物
  let inNotice = false; // 是否已进入“本周告示”区块
  let currentGroup = null; // 当前正在填充的系别分组

  for (const text of paragraphs) {
    // 第一步：先定位“本周告示”段落，之后的内容才是我们要的
    if (!inNotice) {
      if (text.includes("本周告示")) inNotice = true;
      continue;
    }

    // 第二步：遇到下一个大标题（四、五、…）说明告示区结束，立即停止
    if (SECTION_END_RE.test(text)) break;

    // 第三步：解析“参赛系别”段落，开启一个新的周期分组
    let m = TYPE_PERIOD_RE.exec(text);
    if (m) {
      currentGroup = {
        period: m[1], // 如“本周五”“下周二”
        types: splitNames(m[2]), // 参赛系别列表
        pets: [], // 稍后填入该周期的禁赛宠物
      };
      categoryBans.push(currentGroup);
      continue;
    }

    // 第四步：解析天梯禁赛段落。
    // 必须放在系别“禁赛宠物：”判断之前，因为两者都含“禁赛宠物”字样，
    // 这里靠“包括”和冒号来区分。
    m = LADDER_BAN_RE.exec(text);
    if (m) {
      ladderBans = splitNames(m[1]);
      currentGroup = null; // 天梯与系别无关，重置分组避免误挂
      continue;
    }

    // 第五步：解析系别禁赛名单，挂到最近一次出现的周期分组下
    m = CATEGORY_BAN_RE.exec(text);
    if (m && currentGroup !== null) {
      currentGroup.pets = splitNames(m[1]);
      continue;
    }
  }

  // 从网址中提取公告编号，作为唯一主键
  const idMatch = ISSUE_ID_RE.exec(issue.url);
  const issue_id = idMatch ? idMatch[1] : issue.url;

  // 解析该期的“生效周五”，并换算出它覆盖的周次区间 [周五, 下周四]。
  // 这两个字段供上层挑选“当前周次对应的一期”，也方便调用方核对数据属于哪一周。
  const effective_date = parseEffectiveDate(issue.title, issue.date);
  const week_start = effective_date; // 周次起点：生效的那个周五
  const week_end = effective_date ? addDays(effective_date, 6) : null; // 周次终点：下周四

  return {
    issue_id,
    title: issue.title,
    url: issue.url,
    publish_date: issue.date, // 列表页给出的发布日期
    effective_date, // 生效日期（该期的周五）
    week_start, // 周次起点（周五）
    week_end, // 周次终点（下周四）
    category_bans: categoryBans, // 系别排位赛禁赛（含周期、系别、宠物）
    ladder_bans: ladderBans, // 天梯禁赛宠物
  };
}

// ============================== 采集编排 ==============================

/**
 * 读取历史索引，并保证返回的一定是数组。
 * 为什么需要它：线上 KV 里可能残留早期误写入的“非数组”数据（实测 index 键里存的是单个对象），
 * 直接对它调用 index.filter / for...of 会抛异常导致接口 500。
 * 这里统一做兼容与自愈：是数组就用；是单个对象就包成单元素数组；其余情况一律当空处理。
 *
 * @param {object} env Worker 环境变量
 * @returns {Promise<Array>} 索引数组（绝不返回非数组）
 */
async function readIndex(env) {
  const raw = await env.ROCO_BAN.get("index");
  if (!raw) return [];
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return []; // 索引损坏时重建，不影响单期主数据
  }
  if (Array.isArray(parsed)) return parsed;
  if (parsed && typeof parsed === "object") return [parsed]; // 兼容“单个对象”的脏数据
  return [];
}

/**
 * 取得某一期的“生效日期”，取不到则返回 null。
 * 为什么单独抽出来：新数据已带 effective_date 字段；而线上历史 index/KV 没有该字段，
 * 但带了 title + publish_date，可以现场重算，省去为加字段而迁移历史数据。
 */
function issueEffectiveDate(item) {
  if (item.effective_date) return item.effective_date;
  if (item.title && item.publish_date) return parseEffectiveDate(item.title, item.publish_date);
  return null;
}

/**
 * 从若干期公告中挑出“当前周次（周五~下周四）”对应的那一期。
 * 对应 Python 版 scraper.select_current_issue。
 *
 * 规则：在生效日期 <= 今天的所有期里，取生效日期最大的那一期；
 * 若全部期都还没生效，则兜底取生效日期最小的一期（最近将要生效的一期）。
 *
 * @param {Array} items 若干期数据（含 effective_date，或含 title/publish_date 供现场推算）
 * @param {string} [today] 基准日期（YYYY-MM-DD），默认取北京时间当天
 * @returns {object|null} 命中的那一期；无可用数据时返回 null
 */
function selectCurrentIssue(items, today) {
  if (!items || items.length === 0) return null;
  const base = today || beijingTodayIso();

  // 先给每期算出生效日期，算不出来的直接丢弃，不参与选期
  const dated = [];
  for (const item of items) {
    const eff = issueEffectiveDate(item);
    if (eff) dated.push([eff, item]);
  }
  if (dated.length === 0) return null;

  // 已生效的期（ISO 日期字符串可直接按字典序比较）
  const effective = dated.filter((pair) => pair[0] <= base);
  if (effective.length > 0) {
    effective.sort((a, b) => String(b[0]).localeCompare(String(a[0]))); // 生效日期最大者在前
    return effective[0][1];
  }

  // 兜底：全部期都在未来，取生效日期最小的一期（最近将要生效的一期）
  dated.sort((a, b) => String(a[0]).localeCompare(String(b[0])));
  return dated[0][1];
}

/**
 * 执行一次完整的采集并写入 KV。
 * 对应 Python 版 scraper.update_once。
 *
 * 为什么要抓多期：官网有时一次发布多期（例如 09-23 当天同时发了 9/25 与 10/2 两期）。
 * 只抓最新一期会漏掉“当前周次”真正在用的那期，导致跨周时无数据可用。
 *
 * @param {object} env Worker 环境变量，通过 env.ROCO_BAN 访问 KV
 * @returns {Promise<object>} { status, message, data? }
 */
async function updateOnce(env) {
  // 1. 抓列表页，取最近 RECENT_ISSUE_LIMIT 期“更新速递”
  const listHtml = await fetchHtml(LIST_URL);
  const allIssues = parseIssueList(listHtml, ISSUE_TITLE_KEYWORD);
  if (allIssues.length === 0) {
    // 找不到通常意味着官网改版或关键词变了，需要人工介入
    return {
      status: "not_found",
      message: "列表页未发现含“更新速递”的公告",
    };
  }
  const issues = allIssues.slice(0, RECENT_ISSUE_LIMIT);

  // 2. 逐期抓详情页并解析、入库。
  //    单期失败不影响其他期；两类禁赛都为空时跳过，避免用空数据覆盖历史里的同期数据。
  const saved = [];
  const fetchedAt = new Date().toISOString();
  for (const issue of issues) {
    let data;
    try {
      const detailHtml = await fetchHtml(issue.url);
      data = parseIssueDetail(detailHtml, issue);
    } catch (e) {
      continue; // 这一期抓取失败，继续处理下一期
    }
    if (data.category_bans.length === 0 && data.ladder_bans.length === 0) continue;

    data.fetched_at = fetchedAt;
    await env.ROCO_BAN.put(`issue:${data.issue_id}`, JSON.stringify(data));
    saved.push(data);
  }

  if (saved.length === 0) {
    // 抓到了公告，但没有任何一期能解析出内容，多半是文案模板变了，需要人工检查
    return {
      status: "parse_empty",
      message: "找到公告但全部解析为空，可能是文案格式变化，请检查解析规则",
    };
  }

  // 3. 维护历史索引（概要列表），把本次入库的期合并进去，按发布日期倒序，最多 52 期（约一年）。
  //    索引里额外存一份 effective_date，供 /api/ban/current 选期时直接使用。
  let index = await readIndex(env);
  for (const data of saved) {
    index = index.filter((item) => item.issue_id !== data.issue_id); // 去掉同期的旧记录
    index.push({
      issue_id: data.issue_id,
      title: data.title,
      publish_date: data.publish_date,
      effective_date: data.effective_date,
      url: data.url,
      fetched_at: data.fetched_at,
    });
  }
  index.sort((a, b) => String(b.publish_date).localeCompare(String(a.publish_date)));
  index = index.slice(0, 52);
  await env.ROCO_BAN.put("index", JSON.stringify(index));

  // 4. 从本次入库的期里挑出“当前周次”那一期作为返回结果
  const current = selectCurrentIssue(saved);
  return { status: "ok", message: `抓取并入库 ${saved.length} 期成功`, data: current };
}

// ============================== HTTP 路由 ==============================

// 跨域头：允许任意来源调用，方便前端/其他程序直接请求
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

async function handleRequest(request, env) {
  const url = new URL(request.url);
  const path = url.pathname;

  // 预检请求（浏览器跨域会先发 OPTIONS）
  if (request.method === "OPTIONS") {
    return new Response(null, { headers: CORS_HEADERS });
  }

  // 健康检查
  if (path === "/health") {
    return jsonResponse({ status: "ok" }, 200, CORS_HEADERS);
  }

  // 当前周次（周五~下周四）对应的禁赛数据。
  // 不直接返回“最新发布的一期”：官网有时一次发多期，最新的那期可能还没生效。
  // 这里读 index（全部期的概要），按“生效日期 <= 今天取最大”选出目标期，再取该期完整数据。
  if (path === "/api/ban/current") {
    const index = await readIndex(env);
    const chosen = selectCurrentIssue(index);
    const raw = chosen ? await env.ROCO_BAN.get(`issue:${chosen.issue_id}`) : null;
    if (!raw) {
      return jsonResponse(
        { detail: "暂无数据，请先调用 POST /api/ban/refresh 抓取" },
        404,
        CORS_HEADERS
      );
    }
    return new Response(raw, {
      headers: { ...CORS_HEADERS, "Content-Type": "application/json; charset=utf-8" },
    });
  }

  // 历史期数概要（limit 限制在 1~52）
  if (path === "/api/ban/history") {
    let limit = parseInt(url.searchParams.get("limit") || "10", 10);
    if (Number.isNaN(limit)) limit = 10;
    limit = Math.min(Math.max(limit, 1), 52);

    const items = (await readIndex(env)).slice(0, limit);
    return jsonResponse({ count: limit, items }, 200, CORS_HEADERS);
  }

  // 指定期数详情
  const issueMatch = path.match(/^\/api\/ban\/issues\/([^/]+)$/);
  if (issueMatch) {
    const issueId = decodeURIComponent(issueMatch[1]);
    const raw = await env.ROCO_BAN.get(`issue:${issueId}`);
    if (!raw) {
      return jsonResponse(
        { detail: `未找到编号为 ${issueId} 的公告` },
        404,
        CORS_HEADERS
      );
    }
    return new Response(raw, {
      headers: { ...CORS_HEADERS, "Content-Type": "application/json; charset=utf-8" },
    });
  }

  // 手动触发抓取
  if (path === "/api/ban/refresh" && request.method === "POST") {
    try {
      const result = await updateOnce(env);
      return jsonResponse(result, 200, CORS_HEADERS);
    } catch (e) {
      // 网络/HTTP 层错误转成 502，明确告知是上游官网的问题
      return jsonResponse({ detail: `抓取官网失败：${e.message}` }, 502, CORS_HEADERS);
    }
  }

  return jsonResponse(
    {
      detail: "接口不存在",
      available: [
        "GET  /health",
        "GET  /api/ban/current",
        "GET  /api/ban/history?limit=10",
        "GET  /api/ban/issues/{issue_id}",
        "POST /api/ban/refresh",
      ],
    },
    404,
    CORS_HEADERS
  );
}

// ============================== Worker 入口 ==============================

export default {
  /** HTTP 请求入口 */
  async fetch(request, env) {
    return handleRequest(request, env);
  },

  /** 定时任务入口：由 Cron Trigger 触发（每周四北京时间 00:00，即 UTC 周三 16:00） */
  async scheduled(event, env, ctx) {
    // 用 waitUntil 保证抓取任务在后台跑完，不被过早中断
    ctx.waitUntil(updateOnce(env));
  },
};
