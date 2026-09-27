# Fantasy Basketball Assistant

設定驅動、每年重用的本機 Yahoo 拍賣籃球助手。

目前提供設定驗證、資料匯入與快照；投影估值、選秀與季中功能尚未實作。

先安裝 `uv`，執行 `scripts/check` 建立鎖定環境並跑全部檢查。

換季時複製 `examples/2026-27/`，更新規則、賽季日期、來源代碼與快照截止時間，放入名單 CSV。範例聯盟規則的 `assumptions` 必須核對；欄位契約見 `design/schemas/`，由 `src/fba/contracts/` 的型別產生。

```sh
uv run fba build --league /path/league.json --season /path/season.json \
  --model /path/model.json --output /path/snapshots --version 1
uv run fba inspect /path/snapshots/snapshot-SHA256
uv run fba rebuild /path/snapshots/snapshot-SHA256 --output /path/rebuilt
```

名單欄位對應寫在 `season.roster_import.columns`，位置用逗號分隔；報價空白或 `-` 保留缺值。`identities.json` 必須列出明確的 provider ID 對照，程式不按姓名猜配；人工調整可用 `{"format_version":1,"adjustments":[]}`。

抓取失敗會停止；手動來源需設定 `delivery: manual`、`manual_file`、`manual_capture` 的原始抓取時間與 SHA-256。缺少預測、統計或逐場紀錄會明列，不補成零。快照包含設定與原始檔；重建只讀快照且拒絕覆寫。

交付報告、原始資料與快照放在 Git 外。選秀當天操作說明於選秀功能完成時提供。
