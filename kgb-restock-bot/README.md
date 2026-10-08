# Kelab Gasing Beyblade Restock Bot

每 30 分钟检查一次 [kelabgasingbeyblade.my](https://www.kelabgasingbeyblade.my/beyblade-x)
的分类页，有**上新**或**补货**就发 Telegram 通知。

默认监控 `beyblade-x` 分类。

跟同 repo 的 pbandai bot 共用一套触发架构和 Telegram bot，但**代码完全独立**，
互不影响。

---

## 一、核心假设：列表 = 库存

**这个站卖光的商品会从分类页消失。** 所以：

| 状态变化 | 含义 |
|---|---|
| 商品出现在列表上 | 有货 |
| 从列表上消失 | 卖光 |
| 重新出现 | **补货** ← 要通知的就是这个 |
| 从来没见过 | **上新** ← 还有这个 |

不需要打开商品详情页，不需要读库存数字，不需要判断标签文字。
**一个 HTTP GET，一次正则，比对完事。**

跟隔壁 pbandai bot 的对比：

| | pbandai | 这个 |
|---|---|---|
| 依赖 | 无，纯标准库 | 无，纯标准库 |
| 数据从哪来 | 页面里嵌的 `PRELOAD_DATA` JSON | 列表页的 HTML 卡片 |
| 请求数 | 每个站按 `totalCount` 翻到底（SG 现在 6 页） | **1 个列表页** |
| 一次运行 | ~5 秒 | **<1 秒** |

两边现在都不用浏览器了（pbandai 在 2026-10 改成直接读页面里嵌的 JSON）。
差别在于 P-Bandai 卖完的商品**会留在列表上**（挂个 PRE-ORDER CLOSED 标签），
所以必须读每件商品的状态才知道能不能买；这个站卖完就直接从列表上消失，
"在不在列表上"本身就是答案。

---

## 二、抓取失败怎么处理

只有一条规则，但它是整个 bot 最重要的一行：

> **分类页解析出 0 件商品 = 失败，不是"全卖光了"。**

因为"列表 = 库存"这个假设反过来用会出人命：如果把空页面当成真的空，
bot 会把**整个目录**标记成卖光，然后等页面恢复的那一刻，
**把 22 件商品当成补货一次性轰给你**。

所以 0 件一律当抓取失败处理。可能的原因：

- 站点改版，卡片的 class 名变了
- 被 Cloudflare 挡了
- **被塞进虚拟排队页面**——这个站有抢购队列系统，页面上会显示
  「You're #482 in the queue」而不是商品

失败之后的层级：

| 出了什么事 | 后果 |
|---|---|
| 某个分类页读不到 / 解析出 0 件 | 冻结**这个分类**，记录原样保留，其余分类照常 |
| **全部**分类都读不到 | state 一个字节都不动，不 ping healthchecks；运行仍是绿的、带 warning 标注。偶尔一次不用管，一直读不到会在宽限期后告警 |

部分失败**不算运行失败**（exit 0），只在「挂掉」和「恢复」两个时刻各发一次
Telegram，不会每次运行都刷屏。失败分类记在 state 的 `failed_groups` 字段里。

---

## 三、部署

代码和 workflow 都已经在 repo 里了（[`.github/workflows/kgb-check.yml`](../.github/workflows/kgb-check.yml)），
Telegram 的两个 secret 跟 pbandai bot 共用，不用重新设。要做的只有触发和告警：

### 1. Cloudflare Worker

还是那个 Worker（`pbandai-trigger`），三个 bot 共用：

1. **Edit code**：整段换成 [`cloudflare/worker.js`](../cloudflare/worker.js)，**Deploy**
2. **Settings → Trigger events**：只留**一条** `2-59/5 * * * *`，其他的删掉
3. **再 Deploy 一次**，然后**等一个小时再判断有没有问题**。Cloudflare 说触发器改动
   最多 15 分钟生效，实测慢得多：2026-09-03 KGB 的 cron 只加了触发器、没重新部署，
   显示了 Next 时间却从来没触发过；2026-10-08 改成 `2-59/5` 并重新部署后，
   又过了一个多小时才第一次触发

同一个 PAT、同一个 Worker、同一个 repo，不用建新的。

### 2. 掉线告警（可选，但建议）

healthchecks.io 上**新建一个 check**（不要复用 pbandai 那个，否则一个挂了另一个
会替它报平安）：Period `30 minutes`、Grace `30 minutes`（一小时内一次成功都没有才告警）。把 ping URL 存成 GitHub Secret
**`KGB_HEALTHCHECK_URL`**。

不设就是关闭，不影响运行。

### 3. 换分类（可选）

设 repo variable `KGB_WATCH_URLS`，一行一个分类页 URL。不设就用代码里的默认值
（`beyblade-x`）。每次运行 log 开头都会打印实际在监控哪几条。

---

## 四、触发时刻表

唯一的时钟是 Cloudflare Worker：每 5 分钟醒一次，三个 bot 轮流跑，
**P-Bandai 和 KGB 每 30 分钟、Toymana 每 15 分钟**，彼此错开 5 分钟：

| 分钟 | bot |
|---|---|
| `:17` `:47` | P-Bandai（每 30 分钟） |
| `:07` `:37` | KGB（每 30 分钟） |
| `:12` `:27` `:42` `:57` | Toymana（每 15 分钟，三个站里补货最频繁） |

workflow 里没有 GitHub `schedule` 了。它在高负载时会**直接丢弃**任务（实测命中率 13%），
2026-10-08 之前留着当备胎，但三天里一次都没补上过 Cloudflare 漏掉的运行，
全是重复跑，所以拿掉了（详见 [pbandai 的 README](../README.md)）。

想改某个 bot 的频率，改 [`cloudflare/worker.js`](../cloudflare/worker.js) 里它那一行的
`every`（5、10、15、20、30 或 60 分钟）。Cloudflare cron 只用了 1 条（免费版上限 5 条）。

三个 workflow 都会往 `main` 推 state，push 步骤都有 **rebase 重试**（最多 3 次），
各自写各自的 state 文件，所以 rebase 不会冲突。state **只在有变化时才提交**，
光是时间戳变了不算，不然每次运行都会多一个没意义的 commit。

---

## 五、环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | 跟 pbandai bot 共用 |
| `TELEGRAM_CHAT_ID` | ✅ | 跟 pbandai bot 共用，逗号/换行分隔 |
| `KGB_WATCH_URLS` | — | 分类页 URL，换行/逗号分隔。不设 = `beyblade-x` |
| `HEALTHCHECK_URL` | — | healthchecks ping URL（workflow 里由 `KGB_HEALTHCHECK_URL` 传入） |
| `STATE_FILE` | — | 默认 `kgb-restock-bot/state/seen.json` |
| `DRY_RUN` | — | `1` = 只打印不发送，也不 ping healthchecks |

---

## 六、本地跑

```bash
DRY_RUN=1 python3 kgb-restock-bot/check.py
```

没有任何依赖要装。

---

## 七、关于 robots.txt

站点的 `robots.txt` 对 `User-agent: *` 是 **`Allow: /`**，只禁了 `/admin`；
另外单独 `Disallow: /` 了一批 AI 爬虫（ClaudeBot、GPTBot、CCBot、Bytespider 等）
和 `ai-train=no` 信号。

这个 bot 是个人补货监控，每 30 分钟读 **1 个**你本来就会用浏览器打开的公开分类页，
不做训练、不建索引、不转载内容，走的是 `*` 规则。

**30 分钟一次已经够了，别再往上调**——真想第一时间抢到货，正确做法是去看站点
自己的排队系统，而不是把这个脚本改成每分钟跑一次。
