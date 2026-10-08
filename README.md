# P-Bandai Restock Bot

每 30 分钟检查一次 P-Bandai 的商品列表，有**上新**或**补货**就发 Telegram 通知。
只通知**能下单**的商品（PRE-ORDER / IN STOCK / COMING SOON），
OUT OF STOCK 和 PRE-ORDER CLOSED 会自动过滤掉。

默认监控 SG + AU 两个站的 One Piece 系列（`_f_series=03-002`）。

---

## v3（2026-10）：不用浏览器了

**你不用做任何事**，`WATCH_URLS` 不用改，state 会在第一次运行时自动迁移。
下面是改了什么、为什么。

### 为什么改

2026-08-26 起 P-Bandai SG 开始在列表里显示**整个历史目录**，包括所有已截单的预购。
SG 的 One Piece 列表从十几件涨到 103 件，其中能买的只有 2 件。

旧版用浏览器渲染页面，最多翻 5 页（100 件）。列表超过 100 件之后，
**第 101 件开始 bot 就看不到了**，而且它不知道自己没看全。每次 P-Bandai 上新，
最底下那件就被挤出窗口。实测有一件被挤出去之后再也没出现过。
`state/seen.json` 同时涨到了 1500 多行。

### 怎么改的

P-Bandai 的列表页 HTML 里本来就嵌着完整的搜索结果 JSON（`PRELOAD_DATA`），
服务器在任何 JavaScript 运行之前就写进去了：每件商品的编号、名字、价格、
状态标签，还有整个查询的 **`totalCount`**。现在直接读这个：

| | v2（浏览器） | v3（读 JSON） |
|---|---|---|
| 依赖 | Playwright + Chromium | **无，纯标准库** |
| 翻页 | 固定最多 5 页 | 按 `totalCount` **翻到底** |
| 有没有抓全 | 不知道 | **抓到的件数必须等于 `totalCount`**，不等就当失败 |
| 一次运行 | ~75 秒 | **~5 秒** |
| state | 1507 行 | **~30 行** |

商品编号用的是 `productCode`，就是商品 URL 里那一段，跟旧版存的 key 完全一样。
状态标签取的是英文显示名，跟卡片上印的字一致（124 件逐一核对，0 件不同）。
所以升级**不会**误报任何上新。

### state 只记录卖过的商品

旧版把抓到的每件商品都存进 state，能不能买都存。v3 只记录**曾经能下单**的商品：

- 一出现就是截单状态的商品不会存进去
- 存进去之后就一直保留，截单了就标 `present: false`，重新开放会报 **♻️ 补货**
- 第一次运行时自动迁移（`schema: 3`），把旧 state 里现在不能买的记录全部删掉

**代价**：被删掉的那些旧商品里，如果哪件以后重新开放，会报 **🆕 上新**而不是
♻️ 补货。通知照样会收到，只是标签不同。

### 为什么不用 `_f_productStatuses=Waiting,On`

实测 2026-10-05：

| | 不过滤 | `=On` | `=Waiting` | `=Waiting,On` |
|---|---|---|---|---|
| SG | 103 | 2 | 0 | 2 ✅ |
| AU | 21 | 0 | 0 | **21 ❌** |

单个值没问题，但 **`Waiting,On` 组合在两个状态都是 0 件时会被网站直接忽略**，
返回全部商品。AU 现在全部截单，所以拿回整个列表。SG 看起来正常，只是因为
刚好有 2 件在卖，这两件一截单 SG 就会变成跟 AU 一样。

所以 URL **不加状态过滤**，能不能买由脚本根据每件商品自己的状态判断。

---

## 一、拿 Telegram token 和 chat id

1. Telegram 搜 **@BotFather** → `/newbot` → 拿到 `123456789:AAH...` = **BOT_TOKEN**
2. 搜 **@userinfobot** → Start → 它回你的 `Id:` 就是 **CHAT_ID**
3. 记得先给你自己的 bot 点一次 **Start**，否则它没权限给你发消息

### 发给多个人 / 多个群

`TELEGRAM_CHAT_ID` 支持**多个**，逗号或换行分隔：

```
123456789
-1001234567890
987654321
```

- 私聊 id 是**正数**，群组/频道是**负数**，别漏掉减号
- 每个人都要先给 bot 点过 **Start**；群组要先把 bot 拉进群
- `#` 开头的行当注释忽略，可以临时停掉某个收件人
- 某个 id 挂了（对方 block 了 bot、bot 被踢出群）**不会影响其他人收信**，
  只会在 log 里打一行 `delivery failed for: <id>`

---

## 二、部署到 GitHub Actions

1. push 到 GitHub（**包括 `.github/` 和 `state/`**，`.github` 是隐藏文件夹）

   ```bash
   git init && git add -A && git commit -m "init"
   git branch -M main
   git remote add origin git@github.com:<你的用户名>/pbandai-restock-bot.git
   git push -u origin main
   ```

2. Settings → Secrets and variables → Actions → **Secrets**：
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

3. Settings → Actions → General → **Workflow permissions** →
   选 **Read and write permissions** → Save
   （bot 要把 `state/seen.json` commit 回去记住看过哪些商品）

4. Actions 标签页 → *P-Bandai restock check* → **Run workflow** 手动跑一次
   （勾上 **dry_run** 就只打印结果：不发 Telegram，也不提交 state，排查问题用）

### 外部定时触发（Cloudflare Worker）

**别依赖 GitHub 自己的 cron。** 它是 best-effort 的，高负载时不只是延迟，
会直接把任务丢掉不跑（本 repo 实测：`0 * * * *` 触发 0/5 次，`23 * * * *` 2/15 次）。
付费账号也一样，这不是免费额度的问题——public repo 的 Actions 本来就无限免费。

所以 workflow 里**没有** `schedule`，唯一的时钟是一个 **Cloudflare Worker**：
每 5 分钟醒一次，三个 bot 轮流调 GitHub API 触发 `workflow_dispatch`：
**P-Bandai 和 KGB 每 30 分钟、Toymana 每 15 分钟**。dispatch 事件不走那个会丢包的排程队列，叫了就跑。

| 分钟 | bot |
|---|---|
| `:02` `:32` | P-Bandai（每 30 分钟） |
| `:07` `:37` | KGB（每 30 分钟） |
| `:12` `:27` `:42` `:57` | Toymana（每 15 分钟，三个站里补货最频繁） |

错开 5 分钟是为了让三个 bot 不会同时往 main 推 state。

（2026-10-08 之前是每个 bot 每小时一次，另外每个 workflow 还有一个 GitHub `schedule`
当备胎。10-05 到 10-08 三天里，三个备胎合计跑了 34 次，**没有一次**是 Cloudflare
漏掉、靠它补上的，全是重复跑，所以拿掉了。）

Worker 代码在 [`cloudflare/worker.js`](cloudflare/worker.js)
（Cloudflare Dashboard → Workers & Pages → `pbandai-trigger`）。这个 repo 里的
**三个 bot 共用这一个 Worker**，按触发的分钟数决定这一轮该谁跑。
改代码就改那个文件，然后整段粘贴进 Cloudflare 编辑器、Deploy。

**想改某个 bot 的频率**，改 `worker.js` 里它那一行的 `every`
（5、10、15、20、30 或 60 分钟），别的不用动。

Worker 的 Settings 里要配两样（**加了 cron 之后要再 Deploy 一次**，否则只显示 Next 时间、不会真的触发。部署后可能要**一个小时左右**才开始触发，Cloudflare 自己说的 15 分钟不准）：

| 项目 | 值 |
|---|---|
| Cron Triggers | **只要一条**：`2-59/5 * * * *`（三个 bot 共用，每 5 分钟一次） |
| Variables and Secrets | `GITHUB_PAT`（类型选 **Secret**，不是 Text） |

`GITHUB_PAT` 是 GitHub 的 **fine-grained PAT**（Settings → Developer settings →
Personal access tokens → Fine-grained tokens）：Repository access 只勾
`pbandai-restock-bot` 这**一个** repo，Permissions 只给 **Actions: Read and write**。
权限收窄到这个程度，就算泄露了别人最多也只能触发你查一次补货。

几个坑：

- **`User-Agent` header 必须有**，GitHub API 不带会直接拒
- 成功返回 **204 No Content**，没有 body，别以为失败了
- Worker 里**不要写 `fetch` handler**。写了等于开一个公开网址，谁访问一下就触发一次。
  编辑器右边 Preview 面板报 `No fetch handler!` 是**正常的**，不是错误
- 改完代码要点 **Deploy** 才生效；测试用编辑器上方的 **Schedule** 标签手动触发。
  Worker 按**分钟数**决定该谁跑，手动触发时如果那一分钟没轮到任何 bot，
  日志会显示 `nothing due`，这是正常的

**⚠️ PAT 会过期。** 到期那天 dispatch 开始返回 401，Worker 只在 console 里打一行日志，
**不会通知你**，而且现在没有 GitHub 备胎了，**三个 bot 会一起停**，
只能靠下面的掉线告警发现。到期日记进日历，换 token 时只需要更新
Worker 的 `GITHUB_PAT` secret，别的都不用动。

怎么确认它还活着：GitHub Actions 页面看运行记录，正常情况下每 30 分钟应该有一条
`workflow_dispatch`。连续几小时空白就是 Worker 或 PAT 出问题了。不想靠肉眼盯，
就配下面的掉线告警。

### 掉线告警（dead man's switch）

前面那些失败模式里，有一类是**这套系统自己报不了的**：PAT 过期、Worker 挂了、
GitHub runner 根本没启动。脚本压根没跑起来，谈何发通知？GitHub 的失败邮件也只在
workflow **跑了并且失败**时才发——彻底没跑是不会有任何动静的。

解法是反过来：**让脚本定时报平安，超时没报就告警**。用 healthchecks.io（免费）：

1. 注册 → **Add Check** → 起名 `pbandai-restock`
2. **Period** 设 `30 minutes`，**Grace Time** 设 `30 minutes`
   （偶尔漏一次不会吵你，**一小时内一次成功都没有**才告警。
   设成 `1 hour` / `1 hour` 也能用，只是要两小时没动静才告警）
3. 复制它给的 ping URL（形如 `https://hc-ping.com/<uuid>`）
4. 存进 GitHub repo → Settings → Secrets and variables → Actions → **Secrets** →
   `HEALTHCHECK_URL`

**这个 URL 要当密码看**，谁拿到都能替你报平安，把告警骗过去，所以放 Secrets 不是 Variables。

脚本只在这几种情况下 ping：

| 这次运行 | ping | 结果 |
|---|---|---|
| 正常跑完（包括只有部分站点读不到） | 裸 URL | 报平安 |
| **所有站点都读不到** | **不 ping** | 见下面 |
| 配置写错（exit 2）或程序崩溃 | `/fail` | 立刻告警，body 带上 exit code 或完整 traceback |

"所有站点都读不到"故意不报警，也不报平安。这种情况通常是对方网站或网络抖了一下，
下一次就好了（2026-10-06 10:48 就有一次，前后两次都正常）。检查得越频繁，
这种偶发情况就越多，如果每次都立刻告警，你很快就会开始无视告警。
如果是一直读不到（比如 P-Bandai 改版），ping 停了，过了宽限期 healthchecks 会自己变红。
这次运行在 GitHub 上仍然是绿的，但会带一个 **warning 标注**，点进去能看到。

**程序崩溃**就不一样了，那是 bug，所以照样立刻 `/fail`。

覆盖到的情况：

| 出了什么事 | 谁来告诉你 |
|---|---|
| PAT 过期 / Worker 挂了 / runner 没起来 | **healthchecks 超时告警**（只有这个能报） |
| 所有站点一直读不到（比如 P-Bandai 改版） | **healthchecks 宽限期过后告警**（偶尔一次读不到不会吵你） |
| 配置写错（exit 2） | `/fail` ping + GitHub 失败邮件 |
| Python 崩溃 | `/fail` ping（body 里有 traceback）+ GitHub 失败邮件 |

两个设计上的取舍：

- **不设 `HEALTHCHECK_URL` 就自动关闭**，整个功能是可选的
- **ping 失败绝不影响主流程**——只往 stderr 打一行日志，不改 exit code。
  因为"ping 挂了"本身比"因为 ping 挂了导致整个 bot 挂了"轻得多
- **`DRY_RUN=1` 不会 ping**，免得你本地调试一下就把线上的告警给压住了

---

## 三、注意事项

- **免费额度**：public repo 的 Actions 免费无限，**这个 repo 必须保持 public**。
  private repo 每月只有 2000 分钟，而且每次运行至少按 1 分钟算：三个 bot 加起来
  每小时 8 次，一个月约 5800 次运行，远超免费额度。（没有敏感信息，token 都在 Secrets 里。）
- **每次运行都是 Cloudflare Worker 打过来的** `workflow_dispatch`，workflow 里
  没有 `schedule`。原因和配置见上面「外部定时触发」。
- **两次运行挨得很近也不会打架**（比如手动 Run workflow 刚好碰上定时那次）：
  `concurrency` 会让后到的那次排队等前一次跑完；checkout 指定了 `ref`，后到的那次
  拿的是前一次写完之后的 state。（2026-10-06 之前没指定 `ref`，checkout 拿的是运行
  *创建*时的提交：两次几乎同时被创建的话，后一次会从旧 state 开始比对，
  同一条补货可能通知两遍。）
- **state 只在有变化时才提交**。每次运行都会刷新 `last_seen` 和 `updated_at`，
  但如果只有这两个时间戳变了，文件就不写，workflow 也就没东西可提交。
  不然三个 bot 加起来一天会产生近 200 个 commit。所以这两个字段
  现在的意思是"上一次 state 有实质变化的时间"。
- **仍然不保证分钟级准时**：Cloudflare 的 cron 偶尔也会晚个一两分钟，
  只是不像 GitHub 那样整点直接丢掉不跑。真要抢秒杀级别的限量，
  这套（连同任何定时轮询的方案）都不够，得自己拿机器盯。
- **抓取失败按站点隔离**：一个站算"抓成功"必须同时满足：每一页都有
  `PRELOAD_DATA`、翻页过程中 `totalCount` 没变、抓到的件数**正好等于** `totalCount`、
  而且不是 0（系列页永远会列出东西，哪怕全是截单的）。任何一条不满足 →
  **只冻结这个站**，它的记录原样保留（不会被判成消失，恢复后也不会
  误报一堆补货），其余站照常比对、照常发通知。**一个站挂掉不会连累别的站。**
  失败站点记在 state 的 `failed_regions` 字段里。
- **部分失败不算运行失败**（exit 0）。好的站点已经正常比对并通知过了，
  Telegram 也已经告诉你哪个站挂了，再让 healthchecks 每次都变红没有新信息。
  Telegram 只在「挂掉」和「恢复」两个时刻各发一次，不会每次运行都刷屏。
- **全部站点都抓不到**：state 一个字节都不动，不 ping healthchecks，
  运行本身仍是绿的、带一个 warning 标注。偶尔一次不用管；一直这样的话，
  healthchecks 过了宽限期会告警（见上面「掉线告警」）。
- **bot 悄悄停掉是最危险的情况**（PAT 过期、Worker 挂了），因为没有任何东西会报错。
  配了「掉线告警」才有兜底，强烈建议配上。
- **AU 站目前 0 件可下单**（21 件全部售完/预购截止），所以短期内只会收到 SG 的通知。
  这是正常的，不是 bot 坏了。

### 抓取逻辑已在真实页面验证（2026-10-05）

| 页面 | `totalCount` | 抓到 | 翻页 | 可下单 |
|---|---|---|---|---|
| SG One Piece | 103 | 103 | 6 页 | 2 件（PRE-ORDER） |
| AU One Piece | 21 | 21 | 2 页 | 0 件（PRE-ORDER CLOSED / OUT OF STOCK） |

`PRELOAD_DATA` 里只有搜索结果本身，页面底部的推荐位轮播（一堆高达）
不在里面，不需要再特意排除。

---

## 四、本地跑（调试用）

没有任何依赖要装，Python 3.9+ 就行：

```bash
export WATCH_URLS='https://p-bandai.com/sg/series/onepiece-series?_f_series=03-002&offset=0&limit=20&sortType=NewArrival
https://p-bandai.com/au/series/onepiece-series?_f_series=03-002&offset=0&limit=20&sortType=NewArrival'

DRY_RUN=1 python check.py     # 只打印不发消息
```

---

## 五、换别的系列

在网站上筛选好，复制地址栏 URL 放进 `WATCH_URLS`（多条换行分隔）。
**把 `_f_productStatuses=...` 删掉**。`Waiting,On` 在没有匹配商品时会被网站直接忽略（见上面 v3 说明），而且脚本本来就会自己判断能不能买。

---

## 六、环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | BotFather 给的 token |
| `TELEGRAM_CHAT_ID` | ✅ | 一个或多个 chat id，逗号/换行分隔。群组是负数 |
| `WATCH_URLS` | ✅ | 监控的列表页 URL，换行/逗号分隔。`#` 开头的行当注释忽略 |
| `HEALTHCHECK_URL` | — | healthchecks.io 的 ping URL。不设=关闭掉线告警。当密码看，放 Secrets |
| `ALERT_ON_ALL` | — | `1` = 连售完的也通知（默认只通知能下单的） |
| `STATE_FILE` | — | 默认 `state/seen.json` |
| `MAX_PAGES` | — | 翻页上限，默认 50。只是防止失控，不是抓取窗口：列表超过 `MAX_PAGES × limit` 件时整个站当失败，**不会悄悄截断** |
| `REQUEST_DELAY` | — | 翻页之间等几秒，默认 0.5 |
| `DRY_RUN` | — | `1` = 只打印不发送（Actions 页面手动运行时勾 `dry_run` 也是这个效果） |

---

## 七、什么算「能下单」

用排除法：标签里含下面任一关键词就跳过，其余全部通知。
这样即使 Bandai 出了个没见过的新标签，也不会被误杀。

另外 P-Bandai 自己的销售状态 `saleStatus` 是 **`End`** 的也跳过，不管标签写什么。
目前所有 `End` 的商品同时也带 CLOSED / OUT OF STOCK 标签，所以这条现在不改变任何结果；
它是防将来哪件商品已经结束销售、却没带标签，被排除法当成"能买"放过去。

```
OUT OF STOCK / SOLD OUT / CLOSED / NO LONGER AVAILABLE /
END OF SALE / SALE ENDED / ENDED / SUSPENDED / CANCELLED / NOT AVAILABLE
```

改的话见 `check.py` 里的 `UNAVAILABLE_MARKERS`。
