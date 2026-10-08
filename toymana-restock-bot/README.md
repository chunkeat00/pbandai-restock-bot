# Toymana Restock Bot

每 15 分钟检查一次 [Toymana](https://www.toymana.com/collections/beyblade) 的 Beyblade 分类，
有**上新**或**补货**就发 Telegram 通知。

**Toymana 是新加坡的店，价格是 SGD**，通知里显示成 `SG$26.95`。

跟同 repo 的 pbandai、KGB 两个 bot 共用一套触发架构和 Telegram bot，
但**代码完全独立**，互不影响。

---

## 一、数据从哪来

Toymana 是 Shopify 店。Shopify 的每个分类都有现成的 JSON 接口：

```
https://www.toymana.com/collections/beyblade/products.json
```

每件商品的每个规格都带 `available: true/false`，跟网页上看到的是同一份数据。
不用渲染页面，也不用解析 HTML，**一个请求拿到全部 80 件**（每页最多 250 件）。

**robots.txt**：Shopify 默认规则禁止的是带 `sort_by=` 的分类页
（比如浏览器地址栏里的 `?sort_by=most-relevant`），`products.json` 是允许的。
所以 `TOYMANA_WATCH_URLS` 里贴带 `sort_by` 的地址也没关系，脚本只取分类名，
查询参数一律丢掉。

**币种**：每次运行顺手读一下店铺的 `/meta.json` 拿币种。读不到就只显示数字，
**不影响抓取，也不算失败**。

---

## 二、跟另外两个 bot 有什么不同

三个站"卖完了"的表现都不一样，所以判断方式和记录方式也不一样：

| | 卖完的商品 | 怎么判断有货 | state 记录什么 |
|---|---|---|---|
| KGB | **从列表消失** | 在不在列表上 | 列表上的全部 |
| P-Bandai | 留着，挂 PRE-ORDER CLOSED | 每件商品的状态 | **只记卖过的**（截单的大多是多年前的死货） |
| **Toymana** | 留着，`available: false` | 每个规格的 `available` | **全部记录** |

Toymana 全部记录，是因为 2026-10-05 看到的 59 件售罄商品**全是当月才更新过的**，
是刚卖完、会补货的现货，不是停产的死货。**补货就是这个站最常见的事件。**

如果像 P-Bandai 那样只记录卖过的商品，那么 bot 启动时已经售罄的商品，
以后补货就会被错标成 🆕 上新，而实际应该是 ♻️ 补货。
代价是 state 大约 700 行，但每一条记录都是可能补货的商品。

---

## 三、什么算「有货」

**任意一个规格 `available: true`** 就算这件商品有货。

多规格的商品（比如发射器有 Pink / Green / Orange 三个颜色），通知里会列出
**哪几个颜色有货**，价格取有货规格里最便宜的那个：

```
🔹 Beyblade X BXG-62 BX-00 String Launcher
SG$12.95 · 有货规格：Green / Orange
https://www.toymana.com/products/...
```

**已知限制**：判断是按商品，不是按规格。如果 Green 一直有货，后来 Pink 也补货了，
**不会**单独通知，因为这件商品从头到尾都是"有货"。目前 80 件里只有 1 件是多规格的。

---

## 四、抓取失败怎么处理

跟另外两个 bot 同一套规则：**看不到货架 ≠ 货架空了**。

| 出了什么事 | 后果 |
|---|---|
| 某一页没拿到商品列表（网络错误、被拦截、店铺密码页） | 这个分类**整个冻结**，记录原样保留 |
| 商品列表是空的 | 当失败，**不当成全部下架**，否则恢复时会把所有商品当补货轰一遍 |
| 商品超过 `MAX_PAGES × 250` 件 | 当失败并提示调大 `MAX_PAGES`，**不会只读前面一部分** |
| 只有部分分类失败 | 冻结失败的那些，其余照常；Telegram 在"挂掉"和"恢复"时各发一次 |
| 全部分类都失败 | state 一个字节都不动，不 ping healthchecks；运行仍是绿的、带 warning 标注。偶尔一次不用管，一直失败会在宽限期后告警 |

Shopify 不返回商品总数，所以判断"抓全了没有"靠的是**最后一页不满 250 件**。
满 250 就继续往下翻，直到出现不满的一页。

---

## 五、部署

代码和 workflow 都已经在 repo 里了
（[`.github/workflows/toymana-check.yml`](../.github/workflows/toymana-check.yml)），
Telegram 的两个 secret 跟另外两个 bot 共用，不用重新设。

### 1. Cloudflare Worker

还是那个 Worker（`pbandai-trigger`），三个 bot 共用：

1. **Edit code**：整段换成 [`cloudflare/worker.js`](../cloudflare/worker.js)，**Deploy**
2. **Settings → Trigger events**：只留**一条** `2-59/5 * * * *`，其他的删掉
3. **再 Deploy 一次**。这一步别省：触发器改动最多要 15 分钟才生效，
   而且 2026-09-03 KGB 的 cron 只加了触发器、没重新部署，显示了 Next 时间却从来没触发过

同一个 PAT、同一个 Worker、同一个 repo，不用建新的。

### 2. 掉线告警（可选，但建议）

healthchecks.io 上**新建一个 check**（不要复用另外两个 bot 的，否则一个挂了
另一个会替它报平安）：Period `15 minutes`、Grace `45 minutes`（一小时内一次成功都没有才告警）。把 ping URL 存成
GitHub Secret **`TOYMANA_HEALTHCHECK_URL`**。不设就是关闭，不影响运行。

### 3. 换分类、加别的店（可选）

设 repo variable `TOYMANA_WATCH_URLS`，一行一个分类 URL。不设就用默认的
`https://www.toymana.com/collections/beyblade`。

**任何 Shopify 店的分类都能直接用**，不限 Toymana。state 的 key 带店名和分类名
（`toymana.com/beyblade:<商品id>`），不同店、不同分类不会互相干扰。

---

## 六、触发时刻表

唯一的时钟是 Cloudflare Worker：每 5 分钟醒一次，三个 bot 轮流跑，
**P-Bandai 和 KGB 每 30 分钟、Toymana 每 15 分钟**，彼此错开 5 分钟：

| 分钟 | bot |
|---|---|
| `:02` `:32` | P-Bandai（每 30 分钟） |
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

## 七、环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | 跟另外两个 bot 共用 |
| `TELEGRAM_CHAT_ID` | ✅ | 跟另外两个 bot 共用，逗号/换行分隔 |
| `TOYMANA_WATCH_URLS` | — | Shopify 分类 URL，换行/逗号分隔。不设 = Toymana 的 beyblade 分类 |
| `HEALTHCHECK_URL` | — | healthchecks ping URL（workflow 里由 `TOYMANA_HEALTHCHECK_URL` 传入） |
| `STATE_FILE` | — | 默认 `toymana-restock-bot/state/seen.json` |
| `MAX_PAGES` | — | 每个分类最多翻几页（每页 250 件），默认 20。只是防止失控 |
| `REQUEST_DELAY` | — | 翻页之间等几秒，默认 0.5 |
| `DRY_RUN` | — | `1` = 只打印不发送（Actions 页面手动运行时勾 `dry_run` 也是这个效果） |

---

## 八、本地跑

```bash
DRY_RUN=1 python3 toymana-restock-bot/check.py
```

没有任何依赖要装。
