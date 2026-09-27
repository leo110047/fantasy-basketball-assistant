# Fantasy Basketball Assistant

設定驅動、每年重用的本機 Yahoo 拍賣籃球助手。

目前提供年度資料建置、投影估值與固定輸入的回測評分；選秀與季中功能尚未實作。

先安裝 `uv`，執行 `scripts/check` 建立鎖定環境並跑全部檢查。

換季時複製 `examples/2026-27/`，更新規則、賽季日期、來源代碼與快照截止時間，放入名單 CSV。範例聯盟規則的 `assumptions` 必須核對；欄位契約見 `design/schemas/`，由 `src/fba/contracts/` 的型別產生。

```sh
uv run fba annual --league /path/league.json --season /path/season.json \
  --model /path/model.json --output /path/annual --version 1
```

名單欄位對應寫在 `season.roster_import.columns`，位置用逗號分隔；報價空白或 `-` 保留缺值。`identities.json` 必須列出明確的 provider ID 對照，程式不按姓名猜配；人工調整可用 `{"format_version":1,"adjustments":[]}`。

`annual` 一次完成抓取、凍結、投影、估值；失敗不發布半成品。加 `--previous /path/previous-projection` 輸出公允價變化最大的 30 人。

抓取失敗會停止；手動來源需設定 `delivery: manual`、`manual_file`、`manual_capture` 的原始抓取時間與 SHA-256。缺少預測、統計或逐場紀錄會明列。模型中的 `preparation` 明定歷史來源、位置樣本與 OREB 比例估計；這些估計逐人標為 `Assumption`，無可用來源者維持缺值。快照包含設定與原始檔；重建只讀快照且拒絕覆寫。

既有資料快照可用 `project` 直接建置投影與估值。輸入內的來源檔與三份設定都會驗雜湊；產物內保留完整來源快照及逐人處理紀錄。人工調整按公開時間、ID 順序套用；指定出賽數覆寫自動校正，回歸日限制可出賽場數。季中才生效的角色變更需等待季中模擬，不套用整季。

```sh
uv run fba project /path/snapshot-SHA256 --model /path/model.json --output /path/annual
uv run fba calculate /path/projection-input.json --output /path/results
uv run fba evaluate /path/evaluation-input.json --output /path/results
```

這些指令只讀本機輸入，結果記錄設定與輸入雜湊，拒絕覆寫。回測使用實際賽季資料建立固定評分尺度，以重現原始比較；這不代表輸入已有季前公開時間證明。

目前模型不再按球隊總量砍個人分鐘。出賽套用上一季的擬合結果，替補資格依校正前場數判定。舊計算輸入須明確轉換，保留原檔與完整校正快照：

```sh
uv run fba migrate-projection /path/old-input.json --model /path/model.json \
  --calibration-snapshot /path/snapshot-SHA256 --output /path/converted
```

交付報告、原始資料與快照放在 Git 外。選秀當天操作說明於選秀功能完成時提供。
