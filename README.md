# Fantasy Basketball Assistant

設定驅動的本機 Yahoo 拍賣籃球助手。提供年度資料建置、投影估值、本機競標桌、逐日管理重播及預測回測。

需要 `uv` 和 C++17 編譯器。`scripts/check` 建立鎖定的 Python 環境並跑全部檢查。

換季時複製 `examples/2026-27/`，更新規則、日期、來源與快照截止時間，放入 Yahoo CSV、明確的球員 ID 對照及人工調整檔。核對設定裡標記的 `Assumption`；欄位由 `design/schemas/` 定義。報價空白或 `-` 保持缺值，不按姓名猜配球員。

```sh
uv run fba annual --league /path/league.json --season /path/season.json \
  --model /path/model.json --output /path/annual --version 1
```

`annual` 完成抓取、凍結、投影、估值、競標開場與上一季評估，全部成功才發布。`opening-draft.json` 是以通用 Team 1 為我方的空白範本；正式選秀請依下方指令指定席次建立草稿。加 `--previous /path/previous-projection` 比較同季上一版公允價變化最大的 30 人。人工調整檔可從 `{"format_version":1,"adjustments":[]}` 開始。

模型格式 9 以完整 NBA 名單限制整隊預期分鐘；輪替造成的缺賽不計為傷病。`health.injury_share` 拆分其他預期缺賽中的傷病比例，目前 0.5 是未校準假設；已知回歸日前的缺陣另外處理。`team-minutes.json` 列出分配、池外球員與資料缺口。舊投影需從凍結快照重新 `project`。

每次 `project`／`annual` 會產生 `forecast.json`，請保留供下季評估。在下季 `season.sources` 加入 `role: "forecast_archive"`、`adapter: "fba_forecast"`，指定上一季 `season_id` 與此檔的來源、時間、SHA-256；本機存檔使用 `delivery: "manual"`、`file:///` URL、`manual_file` 與 `manual_capture`。缺預測存檔時 `annual` 會停止，可先用 `build`／`project` 完成資料建置，不能宣稱換季驗收完成。

`annual` 的 `auction/` 內已有競標輸入。建立我方草稿，再計算目前狀態；`--mine` 是隊伍編號，從 1 開始，產生的隊名可編輯。

```sh
uv run fba draft-template /path/auction-input-SHA256/auction-input.json \
  --mine 1 --output /path/draft.json
uv run fba serve /path/auction-input-SHA256/auction-input.json \
  --draft /path/draft.json --log /path/auction.jsonl --workers 4
```

開啟 `serve` 顯示的完整網址。指定本輪球員、買家與實際成交價後登錄；可撤銷上一筆、匯出／匯入備份，以及設定我方隊伍與隊名。成交經後端驗證後直接保存到草稿檔，重新整理與重啟服務都會保留。同一草稿一次只開一個服務；每次重啟請使用新網址。

服務運行期間透過介面修改草稿；手動編輯檔案前先停止服務。

每次保存會將被替換的檔案保留在草稿旁的 `.<草稿檔名>.history/`，包含保存間隙的外部修改。需復原時先停止服務、保留現有草稿，再從歷史檔選擇正確版本。歷史檔不會自動刪除；保存需要支援原子交換的 macOS／Linux 本機檔案系統。

市場與預算先更新，停損價在背景重算；「更新中」不顯示舊停損價。計算失敗不影響已保存成交，可按重試。成交若顯示「保存未確認」，先重新整理核對紀錄，避免重複登錄。買／不買比較沿用目前選取模式的估值。依陣容調整把傷兵替補、串流與永久升級納入共同模擬；加人額度與期間讀取聯盟設定，管理策略讀取 `pricing` 與 `management`。停止價仍是局部估計，受傷病與對手策略假設影響，並非實戰勝率。

服務僅限本機、離線讀取凍結輸入。`auction.jsonl` 保存各次計算的草稿、設定／資料雜湊、結果與耗時；瀏覽器 console 的 `fba timing` 記錄畫面完成時間。草稿、日誌與備份請放在專案外。

只需結果檔時：`uv run fba auction /path/auction-input.json --draft /path/draft.json --stage equal --workers 8 --output /path/results`。`--stage` 可選 `market`、`equal`、`fit`。

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
