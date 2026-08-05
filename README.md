# P-Bandai Restock Bot

每小时检查 P-Bandai SG 的商品列表，有**上新**或**补货**就发 Telegram 通知。

默认监控：One Piece 系列（`_f_series=03-002`，状态 `Waiting,On`，按新到货排序）。

---

## 一、拿 Telegram token 和 chat id（约 3 分钟）

1. Telegram 里搜 **@BotFather** → `/newbot` → 起个名字 → 他会给你一串
   `123456789:AAH...` 这就是 **BOT_TOKEN**。
2. 搜到你刚建的 bot，点 **Start**，随便发一句 "hi"。
3. 浏览器打开（把 `<TOKEN>` 换成你的 token）：
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   找 `"chat":{"id":123456789` — 那个数字就是 **CHAT_ID**。

> 想发到群组：把 bot 拉进群，在群里发一句话再看 `getUpdates`，群 id 是负数（例 `-1001234567890`）。

---

## 二、部署到 GitHub Actions

1. 在 GitHub 新建一个 repo，把这个文件夹的内容全部 push 上去
   （**包括 `.github/` 和 `state/`** — `.github` 是隐藏文件夹，别漏了）。

   ```bash
   cd pbandai-restock-bot
   git init && git add -A && git commit -m "init"
   git branch -M main
   git remote add origin git@github.com:<你的用户名>/pbandai-restock-bot.git
   git push -u origin main
   ```

2. repo → **Settings → Secrets and variables → Actions**：
   - **Secrets** 标签页，New repository secret，加两个：
     - `TELEGRAM_BOT_TOKEN`
     - `TELEGRAM_CHAT_ID`
   - **Variables** 标签页（可选）：`WATCH_URLS` — 想监控别的系列时用，
     多个 URL 用换行分开。不设就用默认的 One Piece 那条。

3. repo → **Settings → Actions → General** → 最下面 **Workflow permissions**
   选 **Read and write permissions** → Save。
   （bot 要把 `state/seen.json` commit 回去记住看过哪些商品。）

4. repo → **Actions** 标签页 → 左边选 *P-Bandai restock check* → **Run workflow**
   手动跑一次。第一次跑会发一条「已启动，正在监控 N 件商品」，
   之后只有真的上新/补货才会再通知你。

搞定，之后每小时自动跑。

---

## 三、注意事项

- **免费额度**：public repo 的 Actions 是完全免费无限的。private repo 每月 2000
  分钟，这个 bot 每次约 1-2 分钟 × 每天 24 次 ≈ 每月 900-1400 分钟，够但偏紧。
  **建议把 repo 设成 public**（里面没有敏感信息，token 存在 Secrets 里）。
- **定时会有延迟**：GitHub 的 cron 在高峰期会推迟 5-20 分钟，偶尔跳过一次。
  想更准时/更快就得用自己的服务器。
- **repo 60 天没活动**，GitHub 会自动停掉定时任务并发邮件提醒你，去点一下
  Enable 就恢复。
- **抓不到东西不会清空记录**：如果某次网站改版或被挡，脚本会以 exit code 1
  退出并保留旧状态，不会误报一堆「上新」。这时 GitHub 会给你发一封 workflow
  failed 的邮件——那就是在提醒你去看一眼。（嫌吵可以在 GitHub → Settings →
  Notifications 关掉 Actions 失败邮件。）
- **⚠️ 抓取逻辑还没在真实页面上验证过**：我这边没法访问 p-bandai.com，
  解析逻辑是按通用结构写的（抓所有 `/item/` 链接再往上找卡片）。
  第一次跑完看那条启动消息说监控了几件——如果是 20 件左右就对了，
  如果是 0 件说明被挡或结构不一样，跟我说一声我来调。

---

## 四、本地跑（调试用）

```bash
pip install -r requirements.txt
python -m playwright install chromium

# 只打印不发消息，先看抓得对不对
DRY_RUN=1 python check.py

# 真发
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=xxx
python check.py
```

---

## 五、想监控别的系列？

在 P-Bandai 网站上筛选好，把浏览器地址栏的 URL 复制下来，放进
`WATCH_URLS` 变量即可（多条换行分隔）。例如：

```
https://p-bandai.com/sg/series/onepiece-series?_f_series=03-002&offset=0&limit=20&sortType=NewArrival&_f_productStatuses=Waiting,On
https://p-bandai.com/sg/series/gundam-series?offset=0&limit=20&sortType=NewArrival&_f_productStatuses=Waiting,On
```

---

## 六、环境变量一览

| 变量 | 必填 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | BotFather 给的 token |
| `TELEGRAM_CHAT_ID` | ✅ | 你的 chat id |
| `WATCH_URLS` | — | 要监控的列表页 URL，换行/逗号分隔 |
| `STATE_FILE` | — | 默认 `state/seen.json` |
| `MAX_PAGES` | — | 最多翻几页，默认 5 |
| `DRY_RUN` | — | `1` = 只打印不发送 |
