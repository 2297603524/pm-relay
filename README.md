# pm-relay

给私有看板用的 Polymarket 取数中转。**不需要服务器、不需要 key**，全靠 GitHub Actions 在自己的境外网络里跑。

## 它做什么

| 工作流 | 频率 | 产物 |
|---|---|---|
| `pm-prices` | 每 5 分钟 | `data/pm.json` — 事件 + 合约当前概率 |
| `pm-history` | 每 6 小时 | `data/history/<marketId>.json` — 各合约走势（分时/7天/全部）|

数据全部来自 Polymarket 公开只读接口：`gamma-api.polymarket.com`、`clob.polymarket.com`。

## 怎么开起来

1. **新建一个公开仓库**（例如 `pm-relay`），不要勾选 README / .gitignore，保持空仓库。
2. 把本目录**全部内容**推上去（注意 `.github/` 要在仓库根目录，不能被忽略）：
   ```bash
   cd pm-relay
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<你的用户名>/pm-relay.git
   git push -u origin main
   ```
3. 仓库 **Settings → Actions → General → Workflow permissions** 选
   **Read and write permissions**，保存。（不设这一步，Action 无法把数据提交回来。）
4. 打开 **Actions** 页 → 左侧 `pm-prices` → 右侧 **Run workflow** 手动跑一次。
   约 30 秒后仓库里应出现 `data/pm.json`。
5. 再手动跑一次 `pm-history`（约 3~5 分钟，会生成 `data/history/`）。

## 跑通之后

把仓库地址给看板那边，它会通过 `api.github.com` 读
`repos/<user>/pm-relay/contents/data/pm.json`（匿名 60 次/小时，够用）。
之后就完全脱离原来那个上游站了。

## 本地调试（可选）

```bash
python3 relay/fetch_pm.py prices     # 需要能访问 polymarket
python3 relay/fetch_pm.py history
```

## 说明

- 只读公开数据，不碰任何账户、不需要 API key。
- GitHub 的定时任务最小间隔 5 分钟，且高峰期可能延迟几十秒到几分钟 —— 这是这套方案的精度上限。
- 仓库若设为私有，看板侧匿名读取会 404，必须公开。
