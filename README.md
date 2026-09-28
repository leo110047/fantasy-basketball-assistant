# Fantasy Basketball Assistant

設定驅動的本機 Yahoo 拍賣籃球助手。提供年度資料建置、投影估值、離線競標計算、逐日管理重播及預測回測；操作介面仍待後續階段。

需要 `uv` 和 C++17 編譯器。`scripts/check` 建立鎖定的 Python 環境並跑全部檢查。

換季時複製 `examples/2026-27/`，更新規則、日期、來源與快照截止時間，放入 Yahoo CSV、明確的球員 ID 對照及人工調整檔。核對設定裡標記的 `Assumption`；欄位由 `design/schemas/` 定義。報價空白或 `-` 保持缺值，不按姓名猜配球員。

```sh
uv run fba annual --league /path/league.json --season /path/season.json \
  --model /path/model.json --output /path/annual --version 1
```

`annual` 完成抓取、凍結、投影、估值、競標開場與上一季評估，全部成功才發布。`opening-draft.json` 是以通用 Team 1 為我方的空白範本；正式選秀請依下方指令指定席次建立草稿。加 `--previous /path/previous-projection` 比較同季上一版公允價變化最大的 30 人。人工調整檔可從 `{"format_version":1,"adjustments":[]}` 開始。

每次 `project`／`annual` 會產生 `forecast.json`，請保留供下季評估。在下季 `season.sources` 加入 `role: "forecast_archive"`、`adapter: "fba_forecast"`，指定上一季 `season_id` 與此檔的來源、時間、SHA-256；本機存檔使用 `delivery: "manual"`、`file:///` URL、`manual_file` 與 `manual_capture`。缺預測存檔時 `annual` 會停止，可先用 `build`／`project` 完成資料建置，不能宣稱換季驗收完成。

`annual` 的 `auction/` 內已有競標輸入。建立我方草稿，再計算目前狀態；`--mine` 是隊伍編號，從 1 開始，產生的隊名可編輯。

```sh
uv run fba draft-template /path/auction-input-SHA256/auction-input.json \
  --mine 1 --output /path/draft.json
uv run fba auction /path/auction-input-SHA256/auction-input.json \
  --draft /path/draft.json --stage equal --workers 8 --output /path/results
```

草稿的 `sales` 記錄 `id`、`player_id`、`buyer`、`amount`；撤銷時移除該筆，每次修改增加 `revision`。`market` 只更新市場與預算，`equal` 計算加總停損價，`fit` 加入條件式陣容調整。適配是假設下的局部估值，並非實戰勝率；無解與求解失敗會明示。每份結果綁定設定、輸入及草稿雜湊。

既有快照可完全離線重建；結果拒絕覆寫，請使用新的輸出目錄。

```sh
uv run fba project /path/snapshot-SHA256 --model /path/model.json --output /path/annual
uv run fba prepare-auction /path/projection-input-SHA256 \
  --model /path/model.json --output /path/auction
uv run fba calculate /path/projection-input.json --output /path/results
uv run fba evaluate /path/evaluation-input.json --output /path/results
uv run fba backtest /path/replay-input.json --output /path/results
uv run fba migrate-projection /path/old-input.json --model /path/model.json \
  --calibration-snapshot /path/snapshot-SHA256 --output /path/converted
```

`evaluate` 對已結束賽季輸出排名相關、前 N 命中（N = 隊數 × 名單格數）、價值誤差與出賽誤差。

`backtest` 使用模型格式 6 與 `design/schemas/replay.schema.json`：凍結的競標投影、完整起始名單、自由球員、對戰表、每日決策時間、帶公布時間的傷情和逐場實績。傷情檔是 `{"format_version":1,"observations":[...]}`，實績檔是 `{"format_version":1,"boxes":[...]}`；每筆指定 `source_artifact`，原檔需列入帶來源與雜湊的 `artifacts`。實績必須包含明列的零數據 DNP。輸出逐日先發／IL／加人紀錄、週勝負、排名及季後賽結果；串流與傷兵補人共用設定額度。

歷史季前資料不足時只能標為 `retrospective`，不能宣稱當時已知；目前真實歷史資料仍缺完整公開時間證明。每週輔助的預留介面在 `design/contracts.pyi`。交付報告、原始資料與快照留在 Git 外。
