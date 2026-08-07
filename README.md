# P-Bandai Restock Bot

每小时检查 P-Bandai 的商品列表，有**上新**或**补货**就发 Telegram 通知。
只通知**能下单**的商品（PRE-ORDER / IN STOCK / COMING SOON），
OUT OF STOCK 和 PRE-ORDER CLOSED 会自动过滤掉。

默认监控 SG + AU 两个站的 One Piece 系列（`_f_series=03-002`）。

---

## ⚠️ 升级说明（v2）

如果你已经在跑 v1，这次要改两个地方：

### 1. 换掉 `WATCH_URLS`（重要）

**P-Bandai 的 `_f_productStatuses` 参数不可靠。** 实测 2026-08：

| URL 参数 | AU 站实际返回 |
|---|---|
| `_f_productStatuses=Waiting,On` | 19 件，**全部 OUT OF STOCK / PRE-ORDER CLOSED** ❌ |
| `_f_productStatuses=On` | 0 件（正确，facet 显示 Available = 0）✅ |

`Waiting,On` 这个组合被网站直接无视了。所以 v2 **不再依赖 URL 过滤**，
改成读每张卡片上的状态标签自己判断。你要做的是把状态参数**去掉**：

GitHub repo → Settings → Secrets and variables → Actions → **Variables** →
`WATCH_URLS` 改成这两行：

```
https://p-bandai.com/sg/series/onepiece-series?_f_series=03-002&offset=0&limit=20&sortType=NewArrival
https://p-bandai.com/au/series/onepiece-series?_f_series=03-002&offset=0&limit=20&sortType=NewArrival
```

`WATCH_URLS` 现在是**必填**，代码里没有硬编码的备用 URL。没设就直接 exit 2
并打印怎么设，不会偷偷跑一份你早忘了的旧 URL。每次运行的 log 开头也会打印
实际在监控哪几条，方便核对。

### 2. 建议重置一次 state

v1 会把页面底部 **RECOMMENDATIONS 轮播**里的商品（一堆高达）也当成结果抓进去，
所以你现在的 `state/seen.json` 里有脏数据。v2 已经把抓取范围锁死在结果容器内，
但旧记录还在。清一下：

```bash
git pull
echo '{"initialized": false, "items": {}, "updated_at": null}' > state/seen.json
git commit -am "reset state for v2" && git push
```

下次跑会重新发一条「已启动」的消息，然后恢复正常。
（不清也能跑，只是会留一堆用不到的旧记录。）

state 的 key 格式从 `<id>` 换成了 `<region>:<id>`（这样 SG 和 AU 同号商品不会撞车），
旧记录会自动迁移成 `sg:` 前缀，不用你操心。

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

### 外部定时触发（Cloudflare Worker）

**别依赖 GitHub 自己的 cron。** 它是 best-effort 的，高负载时不只是延迟，
会直接把任务丢掉不跑（本 repo 实测：`0 * * * *` 触发 0/5 次，`23 * * * *` 2/15 次）。
付费账号也一样，这不是免费额度的问题——public repo 的 Actions 本来就无限免费。

真正在按小时干活的是一个 **Cloudflare Worker**，每小时 `:23` 调 GitHub API
触发 `workflow_dispatch`。dispatch 事件不走那个会丢包的排程队列，叫了就跑。
workflow 里的 `schedule` 保留在 `:53` 当兜底，Cloudflare 挂了还有一层。

Worker 代码（Cloudflare Dashboard → Workers & Pages → `pbandai-trigger`）：

```js
export default {
  async scheduled(event, env, ctx) {
    const res = await fetch(
      "https://api.github.com/repos/chunkeat00/pbandai-restock-bot/actions/workflows/check.yml/dispatches",
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
    if (!res.ok) console.log(`dispatch failed: ${res.status} ${await res.text()}`);
  },
};
```

Worker 的 Settings 里要配两样：

| 项目 | 值 |
|---|---|
| Cron Triggers | `23 * * * *` |
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
- 改完代码要点 **Deploy** 才生效；测试用编辑器上方的 **Schedule** 标签手动触发

**⚠️ PAT 会过期。** 到期那天 dispatch 开始返回 401，Worker 只在 console 里打一行日志，
**不会通知你**，bot 就这么悄无声息地停了（GitHub 那个 `:53` 的兜底还在，
但那玩意儿本来就十次有八次不跑）。到期日记进日历，换 token 时只需要更新
Worker 的 `GITHUB_PAT` secret，别的都不用动。

怎么确认它还活着：GitHub Actions 页面看运行记录，正常情况下每小时应该有一条
`workflow_dispatch`。连续几小时空白就是 Worker 或 PAT 出问题了。不想靠肉眼盯，
就配下面的掉线告警。

### 掉线告警（dead man's switch）

前面那些失败模式里，有一类是**这套系统自己报不了的**：PAT 过期、Worker 挂了、
GitHub runner 根本没启动。脚本压根没跑起来，谈何发通知？GitHub 的失败邮件也只在
workflow **跑了并且失败**时才发——彻底没跑是不会有任何动静的。

解法是反过来：**让脚本定时报平安，超时没报就告警**。用 healthchecks.io（免费）：

1. 注册 → **Add Check** → 起名 `pbandai-restock`
2. **Period** 设 `1 hour`，**Grace Time** 设 `1 hour`
   （这样偶尔漏一次不会吵你，连续 2 小时没动静才发邮件）
3. 复制它给的 ping URL（形如 `https://hc-ping.com/<uuid>`）
4. 存进 GitHub repo → Settings → Secrets and variables → Actions → **Secrets** →
   `HEALTHCHECK_URL`

**这个 URL 要当密码看**，谁拿到都能替你报平安，把告警骗过去，所以放 Secrets 不是 Variables。

脚本会打三种 ping：

| 时机 | ping | 作用 |
|---|---|---|
| 开跑 | `/start` | 让 healthchecks 知道这次跑了多久 |
| `exit 0` | 裸 URL | 报平安 |
| 非 0 或崩溃 | `/fail` | 立刻告警，body 带上 exit code 或完整 traceback |

覆盖到的情况：

| 出了什么事 | 谁来告诉你 |
|---|---|
| PAT 过期 / Worker 挂了 / runner 没起来 | **healthchecks 超时告警**（只有这个能报） |
| p-bandai 改版导致抓不到（exit 1） | `/fail` ping + GitHub 失败邮件 |
| 配置写错（exit 2） | `/fail` ping + GitHub 失败邮件 |
| Python 崩溃 | `/fail` ping（body 里有 traceback）+ GitHub 失败邮件 |

两个设计上的取舍：

- **不设 `HEALTHCHECK_URL` 就自动关闭**，整个功能是可选的
- **ping 失败绝不影响主流程**——只往 stderr 打一行日志，不改 exit code。
  因为"ping 挂了"本身比"因为 ping 挂了导致整个 bot 挂了"轻得多
- **`DRY_RUN=1` 不会 ping**，免得你本地调试一下就把线上的告警给压住了

---

## 三、注意事项

- **免费额度**：public repo 的 Actions 免费无限。private repo 每月 2000 分钟，
  这个 bot 每小时约 1-2 分钟 ≈ 每月 900-1400 分钟，够但偏紧。**建议设成 public**
  （没有敏感信息，token 在 Secrets 里）。
- **每小时那一下是 Cloudflare Worker 打过来的**，不是 GitHub 的 cron。
  workflow 里 `:53` 那条 `schedule` 只是兜底，指望不上。原因和配置见上面
  「外部定时触发」。所以运行记录里绝大多数是 `workflow_dispatch` 而不是 `schedule`，
  这是**正常的**。
- **两边都触发也不会打架**：workflow 里的 `concurrency` 会让后到的那次排队等
  前一次跑完，不存在两个 job 同时 `git push` state 的情况。
- **仍然不保证分钟级准时**：Cloudflare 的 cron 偶尔也会晚个一两分钟，
  只是不像 GitHub 那样整点直接丢掉不跑。真要抢秒杀级别的限量，
  这套（连同任何一小时一次的方案）都不够，得自己拿机器盯。
- **repo 60 天没活动**会自动停掉 `schedule`。不过 bot 每次跑都会 commit
  `state/seen.json`，活动一直有，实际不会触发这条；就算真被停了，
  Cloudflare 的 `workflow_dispatch` 也不受影响，照跑。
- **抓取失败按站点隔离**：脚本检查结果容器 `.o-search-product` 在不在。某个站
  找不到 → **只冻结这个站**，它的记录原样保留（不会被判成消失，恢复后也不会
  误报一堆补货），其余站照常比对、照常发通知。**一个站挂掉不会连累别的站。**
  失败站点记在 state 的 `failed_regions` 字段里。
- **部分失败不算运行失败**（exit 0）。好的站点已经正常比对并通知过了，
  Telegram 也已经告诉你哪个站挂了，再让 healthchecks 每小时变红没有新信息。
  Telegram 只在「挂掉」和「恢复」两个时刻各发一次，不会每小时刷屏。
- **只有全部站点都抓不到才 exit 1**，且 state 一个字节都不动。这时 GitHub 会发
  workflow failed 邮件，healthchecks 也会收到 `/fail` ping。
- **bot 悄悄停掉是最危险的情况**（PAT 过期、Worker 挂了），因为没有任何东西会报错。
  配了「掉线告警」才有兜底，强烈建议配上。
- **AU 站目前 0 件可下单**（19 件全部售完/预购截止），所以短期内只会收到 SG 的通知。
  这是正常的，不是 bot 坏了。
- **2026-08-07 起 AU 站开始读不到结果容器**（run #32）。页面本身能正常打开、
  也没被封 IP，就是 `.o-search-product` 等 20 秒也不出现。最可能的原因是
  AU 站该系列**筛选结果变成 0 件**了——页面不渲染结果容器，脚本没法区分
  「0 件」和「抓取失败」，只能当失败处理。这正是上面「按站点隔离」要解决的场景。

### 抓取逻辑已在真实页面验证（2026-08-05）

| 页面 | 抓到 | 可下单 | 状态标签 |
|---|---|---|---|
| SG One Piece | 4 件 | 4 件 | PRE-ORDER |
| AU One Piece | 19 件 | 0 件 | OUT OF STOCK / PRE-ORDER CLOSED |

两个站的推荐位商品都已正确排除（SG 8 件、AU 8 件高达）。

---

## 四、本地跑（调试用）

```bash
pip install -r requirements.txt
python -m playwright install chromium

export WATCH_URLS='https://p-bandai.com/sg/series/onepiece-series?_f_series=03-002&offset=0&limit=20&sortType=NewArrival
https://p-bandai.com/au/series/onepiece-series?_f_series=03-002&offset=0&limit=20&sortType=NewArrival'

DRY_RUN=1 python check.py     # 只打印不发消息
```

---

## 五、换别的系列

在网站上筛选好，复制地址栏 URL 放进 `WATCH_URLS`（多条换行分隔）。
**建议把 `_f_productStatuses=...` 删掉**——反正脚本自己会过滤，留着反而可能触发上面那个 bug。

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
| `MAX_PAGES` | — | 最多翻几页，默认 5 |
| `DRY_RUN` | — | `1` = 只打印不发送 |

---

## 七、什么算「能下单」

用排除法：卡片标签里含下面任一关键词就跳过，其余全部通知。
这样即使 Bandai 出了个没见过的新标签，也不会被误杀。

```
OUT OF STOCK / SOLD OUT / CLOSED / NO LONGER AVAILABLE /
END OF SALE / SALE ENDED / ENDED / SUSPENDED / CANCELLED / NOT AVAILABLE
```

改的话见 `check.py` 里的 `UNAVAILABLE_MARKERS`。
