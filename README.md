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

---

## 三、注意事项

- **免费额度**：public repo 的 Actions 免费无限。private repo 每月 2000 分钟，
  这个 bot 每小时约 1-2 分钟 ≈ 每月 900-1400 分钟，够但偏紧。**建议设成 public**
  （没有敏感信息，token 在 Secrets 里）。
- **定时会延迟**：GitHub cron 高峰期推迟 5-20 分钟，偶尔跳过。
- **repo 60 天没活动**会自动停掉定时任务，去点一下 Enable 就恢复。
- **抓不到会中止，不会清空记录**：脚本会检查结果容器 `.o-search-product`
  在不在。找不到 → exit 1、保留旧状态，不会把整个目录当成「上新」误报一遍。
  这时 GitHub 会发一封 workflow failed 邮件提醒你。
- **AU 站目前 0 件可下单**（19 件全部售完/预购截止），所以短期内只会收到 SG 的通知。
  这是正常的，不是 bot 坏了。

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
| `TELEGRAM_CHAT_ID` | ✅ | 你的 chat id |
| `WATCH_URLS` | ✅ | 监控的列表页 URL，换行/逗号分隔。`#` 开头的行当注释忽略 |
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
