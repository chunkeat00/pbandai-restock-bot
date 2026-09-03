# Kelab Gasing Beyblade Restock Bot

每小时检查 [kelabgasingbeyblade.my](https://www.kelabgasingbeyblade.my/beyblade-x)
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
| 依赖 | Playwright + chromium | **无，纯标准库** |
| 请求数 | 2 个列表页（浏览器渲染） | **1 个列表页** |
| 一次运行 | ~50 秒 | **<1 秒** |

pbandai 那边非要用浏览器，是因为它的页面靠 JS 渲染、而且售完的商品**会留在页面上**
（挂个 PRE-ORDER CLOSED 标签），所以必须读标签文字才知道能不能买。
这个站两个问题都没有。

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
| **全部**分类都读不到 | exit 1，state 一个字节都不动 |

部分失败**不算运行失败**（exit 0），只在「挂掉」和「恢复」两个时刻各发一次
Telegram，不会每小时刷屏。失败分类记在 state 的 `failed_groups` 字段里。

---

## 三、部署

代码和 workflow 都已经在 repo 里了（[`.github/workflows/kgb-check.yml`](../.github/workflows/kgb-check.yml)），
Telegram 的两个 secret 跟 pbandai bot 共用，不用重新设。要做的只有触发和告警：

### 1. Cloudflare Worker 加一条 cron

沿用已有的那个 Worker（`pbandai-trigger`），加第二条 cron 触发器 `38 * * * *`，
然后把代码换成按 `event.cron` 分派：

```js
const REPO = "chunkeat00/pbandai-restock-bot";

// 哪条 cron 触发哪个 workflow。错开分钟数，两个 bot 不会同时往 main 推 state。
const JOBS = {
  "23 * * * *": "check.yml",       // pbandai
  "38 * * * *": "kgb-check.yml",   // beyblade
};

export default {
  async scheduled(event, env, ctx) {
    const wf = JOBS[event.cron];
    if (!wf) return;
    const res = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${wf}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_PAT}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "pbandai-restock-trigger",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );
    if (!res.ok) console.log(`${wf} dispatch failed: ${res.status} ${await res.text()}`);
  },
};
```

同一个 PAT、同一个 Worker、同一个 repo，不用建新的。

### 2. 掉线告警（可选，但建议）

healthchecks.io 上**新建一个 check**（不要复用 pbandai 那个，否则一个挂了另一个
会替它报平安）：Period `1 hour`、Grace `1 hour`。把 ping URL 存成 GitHub Secret
**`KGB_HEALTHCHECK_URL`**。

不设就是关闭，不影响运行。

### 3. 换分类（可选）

设 repo variable `KGB_WATCH_URLS`，一行一个分类页 URL。不设就用代码里的默认值
（`beyblade-x`）。每次运行 log 开头都会打印实际在监控哪几条。

---

## 四、触发时刻表

四个事件均匀分布在一小时里，互不排队、互不抢 push：

| 分钟 | 谁 | 什么 |
|---|---|---|
| `:08` | GitHub schedule | beyblade **备胎** |
| `:23` | Cloudflare Worker | pbandai **主力** |
| `:38` | Cloudflare Worker | beyblade **主力** |
| `:53` | GitHub schedule | pbandai **备胎** |

为什么主力是 Cloudflare 而不是 GitHub 自己的 cron，见
[pbandai 的 README](../README.md)——简单说就是 GitHub 的 `schedule` 会在高负载时
**直接丢弃**任务，实测命中率只有 13%。

两个 workflow 都会往 `main` 推 state，所以 push 步骤都加了 **rebase 重试**
（最多 3 次）。抢输了是正常现象，不是错误。

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

这个 bot 是个人补货监控，每小时读 **1 个**你本来就会用浏览器打开的公开分类页，
不做训练、不建索引、不转载内容，走的是 `*` 规则。

**别把频率调高**——真想第一时间抢到货，正确做法是去看站点自己的排队系统，
而不是把这个脚本改成每分钟跑一次。
