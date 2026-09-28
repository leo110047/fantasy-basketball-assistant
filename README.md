# Fantasy Basketball Assistant

設定驅動的本機 Yahoo 拍賣籃球助手。提供年度資料建置、投影估值、離線競標計算與固定輸入回測；操作介面與完整季中回測仍待後續階段。

需要 `uv` 和 C++17 編譯器。`scripts/check` 建立鎖定的 Python 環境並跑全部檢查。

換季時複製 `examples/2026-27/`，更新規則、日期、來源與快照截止時間，放入 Yahoo CSV、明確的球員 ID 對照及人工調整檔。核對設定裡標記的 `Assumption`；欄位由 `design/schemas/` 定義。報價空白或 `-` 保持缺值，不按姓名猜配球員。

```sh
uv run fba annual --league /path/league.json --season /path/season.json \
  --model /path/model.json --output /path/annual --version 1
```

`annual` 完成抓取、凍結、投影與估值，失敗不發布半成品。加 `--previous /path/previous-projection` 比較公允價變化最大的 30 人。來源失敗會停止；手動檔案必須提供原始抓取時間與 SHA-256。人工調整檔可從 `{"format_version":1,"adjustments":[]}` 開始。

用估值產物準備競標資料，建立空草稿，再計算目前狀態。`--mine` 是隊伍編號，從 1 開始；產生的隊名可編輯。

```sh
uv run fba prepare-auction /path/projection-input-SHA256 \
  --model /path/model.json --output /path/auction
uv run fba draft-template /path/auction-input-SHA256/auction-input.json \
  --mine 1 --output /path/draft.json
uv run fba auction /path/auction-input-SHA256/auction-input.json \
  --draft /path/draft.json --stage equal --workers 8 --output /path/results
```

草稿的 `sales` 記錄 `id`、`player_id`、`buyer`、`amount`；撤銷時移除該筆，每次修改增加 `revision`。`market` 只更新市場與預算，`equal` 計算加總停損價，`fit` 加入條件式陣容調整。適配是假設下的局部估值，並非實戰勝率；無解與求解失敗會明示。每份結果綁定設定、輸入及草稿雜湊。

既有快照可完全離線重建；結果拒絕覆寫，請使用新的輸出目錄。

```sh
uv run fba project /path/snapshot-SHA256 --model /path/model.json --output /path/annual
uv run fba calculate /path/projection-input.json --output /path/results
uv run fba evaluate /path/evaluation-input.json --output /path/results
uv run fba migrate-projection /path/old-input.json --model /path/model.json \
  --calibration-snapshot /path/snapshot-SHA256 --output /path/converted
```

回測評分使用固定的實際賽季尺度；歷史資料尚缺完整的季前公開時間證明。交付報告、原始資料與快照留在 Git 外。
