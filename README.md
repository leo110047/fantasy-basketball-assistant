# Fantasy Basketball Assistant

設定驅動的本機 Yahoo 拍賣籃球助手：建置年度資料、計算估值與停止價、重播季中管理。競標時離線讀取凍結資料，Yahoo 成交需手動登錄。

## 安裝

使用 macOS 或 Linux；需要 `uv`、可由 `c++` 呼叫的 C++17 編譯器。完整測試另需 `node`。

```sh
uv sync --locked
```

Python 版本由 `.python-version` 指定，套件版本由 `uv.lock` 鎖定。以下命令均在專案目錄執行，`/path/...` 請換成實際路徑。資料、草稿、備份與報告放在專案外。

## 每年換季

複製 [examples/2026-27](examples/2026-27/)，更新下列內容。範例未附球員資料或上一季預測存檔，不能直接執行完整年度建置。

| 檔案 | 需確認的內容 |
|---|---|
| `league.json` | 隊數、預算、位置、類別、加人額度、IL、對戰週與季後賽；核對標為 `Assumption` 的規則 |
| `season.json` | 賽季日期、快照截止時間、來源網址與賽季代碼、名單及對照檔路徑 |
| `model.json` | 預測權重、出賽校正、球隊資源限制與管理策略；保留各參數的依據 |

依 `season.json` 指定的位置放入 Yahoo CSV、明確的球員 ID 對照與人工調整檔。報價空白或 `-` 保持缺值；身分對不到時停止並列出名單，不按姓名猜配。無人工調整時使用 `{"format_version":1,"adjustments":[]}`。欄位格式見 [design/schemas](design/schemas/)。

完整年度建置需要上一季的 `forecast.json`：在 `season.sources` 設定唯一的 `role: "forecast_archive"`、`adapter: "fba_forecast"` 來源，指定上一季 `season_id`。本機檔案使用 `delivery: "manual"`、`file:///` URL、`manual_file`，並在 `manual_capture` 記錄抓取時間與 SHA-256。

```sh
uv run fba annual --league /path/league.json --season /path/season.json \
  --model /path/model.json --output /path/annual --version 1
```

成功後輸出投影資料夾，內含快照、估值、`forecast.json`、`previous-evaluation.json`、`auction/` 與加總模式開場結果。保留整個資料夾，尤其是供下一季使用的 `forecast.json`。加上 `--previous /path/previous-projection` 可比較同季上一版公允價變化最大的 30 人。

缺上一季預測存檔時，`annual` 會停止；可先依序執行 `build`、`project`、`prepare-auction` 建立競標輸入，上一季評估仍未完成。各命令參數見 `uv run fba COMMAND --help`。

## 選秀當天

使用 `annual` 產出的 `auction/auction-input-SHA256/auction-input.json`。`--mine` 是從 1 起算的席次；隊名可在介面修改。

```sh
uv run fba draft-template /path/auction-input-SHA256/auction-input.json \
  --mine 1 --output /path/draft.json
uv run fba serve /path/auction-input-SHA256/auction-input.json \
  --draft /path/draft.json --log /path/auction.jsonl --workers auto
```

開啟 `serve` 顯示的完整網址。Yahoo 成交後，指定球員、買家與成交價再登錄。介面支援撤銷、歷史成交更正、價格表 CSV、草稿備份匯出／匯入；快捷鍵及價格欄位說明見「使用說明」。

- 市場與預算先更新，停止價完成前顯示「更新中」。計算失敗時成交仍保留，可重試；若顯示「保存未確認」，先重新整理核對紀錄。
- 成交直接保存到草稿，重載與重啟後保留。同一草稿只開一個服務；手動編輯前先停止服務。重啟後使用新網址。
- `--workers auto` 以可用邏輯 CPU 的四分之三為程序上限，至少 1 個；實際批次數依計算量調整，閒置程序等待。可指定整數降低上限。新成交會取消舊的待辦計算，已開始的工作在檢查點停止。
- 被替換的草稿保留在同層 `.<草稿檔名>.history/`，不自動刪除。復原前先停止服務並保留目前草稿；`.pending` 暫存須核對後才能使用。

`auction.jsonl` 記錄計算狀態、設定／資料雜湊、結果與耗時；瀏覽器 console 的 `fba timing` 記錄畫面完成時間。

## 模型設定與數字解讀

- `team_constraints.minutes`／`offense`：`audit` 只記錄超額，`enforce` 套用校正；範例皆為 `enforce`。兩者都檢查完整隊伍名單，輸出 `team-minutes.json`／`team-offense.json`。輪替缺賽不計為傷病；傷病占比與上季進攻用量仍是假設。
- 依陣容調整共用傷兵替補、串流與永久升級模擬。加人額度與期間讀聯盟設定，策略讀 `pricing`／`management`。停止價是目前預算與陣容下的局部估計，受健康及對手行為假設影響。
- 市場直接計算出價分布；規劃成本取平均取得成本最近的合法整數價，半格進位。波動與財力反應尚未校準，規劃價不保證成交。
- 「串流格數比較」讀 `streaming_comparison.slots`；全部健康分組都改善才建議增加格數。建議不會自動改寫 `pricing.streaming_slots`。球員詳情的健康抽樣範圍是分組敏感度，不是信賴區間。
- 預測評估的「估值誤差」是預測與季末實績按同一把估值尺換算後，兩份前 N 名聯集的金額絕對差中位數；N＝隊數 × 名單格數。它不衡量成交價或停止價準確度。

設定變更後須重新建置對應結果。舊市場設定改用 `market.format_version: 2` 時，刪除 `samples`、`seed`、`normalization_samples`、`normalization_seed` 後重新 `prepare-auction`；投影模型或球隊資源政策變更時，從凍結快照重新 `project`。

## 離線計算與研究

使用新的輸出目錄，避免覆寫既有結果。完整參數見各命令的 `--help`。

| 命令 | 輸入與用途 |
|---|---|
| `rebuild`／`inspect` | 從快照離線重建／檢視來源清單 |
| `project` | 從快照與模型設定建立投影、估值及 `forecast.json` |
| `prepare-auction` | 從投影資料夾建立競標輸入；可重複加入 `--scenario NAME PROJECTION` 比較替代模型 |
| `calculate` | 從 `projection-input.json` 重算估值 |
| `auction` | 從競標輸入與草稿輸出 `market`、`equal` 或 `fit` 結果 |
| `evaluate` | 從評估輸入輸出排名相關、前 N 命中、估值誤差及出賽誤差 |
| `backtest` | 從重播輸入輸出逐日先發、IL、加人紀錄、週勝負與季後賽結果 |
| `auction-paths` | 依研究設定比較買入／跳過後的整場競標路徑 |
| `migrate-projection` | 以明確的新模型與校正快照轉換舊投影輸入 |

預測情境必須共用名單、聯盟、賽季與來源快照，只改模型。公允價範圍顯示在球員詳情，不改動正式價格或停止價。

`backtest` 輸入見 [replay.schema.json](design/schemas/replay.schema.json)。傷情／賽程原檔使用 `observations`，實績原檔使用 `boxes`，皆需 `format_version: 1`，並由 `source_artifact` 對應凍結的來源檔；型別見 [contracts/backtest.py](src/fba/contracts/backtest.py)。實際賽程中的未出場須明列零數據，取消的日期不得填假 DNP。重播格式 2 的 `decision_times` 涵蓋賽季每日，`schedules` 可為空；更新完整替換該球員賽程，保留已結束日期，僅在公布後生效。缺當時公開資料證明時標為 `retrospective`。

`auction-paths` 的研究設定範例為 [auction-stress.json](examples/2026-27/auction-stress.json)，包含 18 組配對情境。`--ceiling` 是本次提名的最高出價；跳過後仍可能再買。後續沿用凍結的局部估值，依公開成交調整預算；任一路徑未填滿全部隊伍即標為未完成，情境比例不是勝率。此研究不在現場成交流程執行。

## 開發與驗證

`scripts/check` 建立鎖定環境，執行格式、lint、型別、死碼、依賴與測試檢查。資料型別由 `src/fba/contracts/` 定義，`design/schemas/` 由型別產生；[design/contracts.pyi](design/contracts.pyi) 引用正式型別，另定義尚未實作的每週輔助介面。

CI 比較同機器上 20 個新狀態的並行與單程序結果，要求完全一致，暖機後並行 p95 不得慢超過 10%。選秀前的絕對秒數驗收另以使用的 Mac 重播 140 筆成交與撤銷；驗收報告不放 Git。

若部署環境禁止從暫存目錄載入原生程式，可在安裝套件 `fba/native/<system>-<machine>/season.so` 放入相同原始碼與工具鏈的產物。啟動仍需本機編譯器重新編譯並逐位元核對，一致才載入；編譯產物不放 Git。
