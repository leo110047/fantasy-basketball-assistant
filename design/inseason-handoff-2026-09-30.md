# 季賽助手交接：未完成工作與驗收缺口

更新日期：2026-09-30。專案：`/Users/leo/fantasy-basketball-assistant`。

## 1. 接手時先知道的事

**整份規格尚未完成，現況不能當成一般使用者 clone 後即可使用的成品。**
最大的阻礙仍是真實 NBA 資料入口、真實規模的計算效能，以及多項預測／模擬／回顧正確性問題。
753 項測試與第一批 Goldband 複審只證明該批候選在指定驗證範圍內通過，不代表下列問題已結案。

權威需求：

- 原規格：`/Users/leo/Downloads/季賽助手：功能規格與驗收標準.md`。
- 共用層原規格：`/Users/leo/Downloads/Fantasy 籃球助手重構規格與驗收條件.md`。
- 本文件涵蓋使用者 2026-09-30 的完整缺陷清單及目前已知的交付缺口。未逐項重做整份規格的驗收，所以未列出新缺陷的條款也不能自動視為通過。
- **交付方式已改為 clone + 雙擊命令檔**，取代安裝檔／打包流程。不要重新加入 PyInstaller、DMG 或 installer CI。
- 使用者要求 commit 分批；本次只授權 commit 與交接，沒有 push、部署、付費訂閱或代操作 Yahoo 的授權。
- Yahoo 維持唯讀，建議由使用者在 Yahoo 手動執行。不得為了測試直接變更真實聯盟。

## 2. 已提交的第一批修正

| Commit | 範圍 |
| --- | --- |
| `e3c8a68` | 共用 Windows socket：exclusive/reuse 衝突、10013／10048 重試；附模擬 socket 測試。 |
| `b560b26` | Yahoo `ST` → 內部 `STL` 對照；既有本機 catalog 也取得新標籤，保留使用者覆寫。 |
| `f2bf74b` | 預測、模擬、建議、擬合、UI 與新舊回顧計分相容性修正，詳下表。 |

這三批是同一個已審查候選的拆分；完整測試證據對應合併後內容，**沒有宣稱每個中間 commit 都各跑完整驗證**。
先前 clone 交付 commit 為 `63749e1`。本次交接文件另批提交；沒有 push。

| 原回報問題 | 本批處理與仍有的界限 |
| --- | --- |
| Yahoo 不認 `ST` | 設定與比分對照已補。涵蓋新／舊本機設定；仍沒有真實 Yahoo 錄製回應驗收。 |
| Windows socket 綁定／錯誤碼 | 程式與分支測試已修；實際 Windows 保留 port、啟動及程序清理仍待驗證。 |
| 換人功能被回測報告鎖住 | 已解除執行時鎖，保留未校準提示與資料／聯盟可用性檢查；回測交付驗收仍未通過。 |
| 零分鐘比賽被丟棄 | 保留於分鐘與角色估計；產出偏離的比率／標準誤公式尚未修，見 M1。 |
| 未使用回歸日 | 未來估計日期會影響未來可出賽；今天／已過期的日期不覆蓋最新傷兵狀態。不是傷病模型已校準的證據。 |
| 背靠背手調被一般狀態覆蓋 | 一般狀態先套用，背靠背專用欄位後套用。 |
| 分鐘重分配扣到傷兵 | 排除出賽機率為 0 的隊友。部分出賽機率、健康但失去輪替等仍需更完整情境驗收，不能概括為所有傷兵配置已完成。 |
| 多群組、雙位置先驗失敗 | 使用符合位置／分鐘群組的 peer 聯集，每位 peer 計一次。預設群組細分與實證依據仍待補。原本單一群組已有位置交集篩選，並非完全無條件的全聯盟平均。 |
| 低數量數據被四捨五入 | 移除 bootstrap 縮放後的取整，補均值及投籃比例測試。未因此證明整個抽樣模型／雙十／尾端機率已校準。 |
| 對手已鎖定先發未遵守 | 基準排陣與最佳化使用共用鎖定條件；已鎖定板凳也不會重新排上。真實 Yahoo 鎖定資料仍待核對。 |
| 整週平手算半勝 | 新預測改為嚴格勝率；排名積分保留聯盟平手設定。舊紀錄預設 `standings_points`，新紀錄明記 `win_probability`，回顧分開評分。 |
| 沒有關鍵類別就不搜尋 | 改用全部類別做候選篩選；完整搜尋品質與效能仍未達標。 |
| 交易要求填滿所有先發位置 | 移除這個錯誤限制；容量、重複球員與已知球員檢查保留。其他真實 Yahoo 交易／IL 合法性仍待整體驗收。 |
| logistic 擬合失敗／分離資料極端係數 | 改用解析梯度、曲率求解 score equations，檢查梯度殘差；拒絕不識別、完全／準完全分離的資料。新增 40 組合成案例通過，**不是使用者原始 40 組**。既有已存擬合報告的適用性也應在後續真實流程核對。 |
| 今日頁預設台北日期 | 預設改用後端聯盟時區的 projection 日期；跨日／DST 的完整 UI 流程仍待驗收。 |
| 提案紀錄未顯示交易內容 | 已加入送出／收到球員欄位；這次只有語法檢查，沒有新的瀏覽器視覺驗收。 |
| 文件隱藏資料來源缺口的後果 | 說明文件與來源設定畫面已明寫：缺少自訂格式來源時，F1–F6 與球員對照均不能初始化，F1/F2 也不例外。 |

## 3. 證據與不能推論的範圍

### 自動驗證與獨立審查

- 最終候選 digest：`66268d101495970fddfba232fb7ea21a9e62fda2088a3008536ecccdcb525d3e`。
- Goldband 完整 `scripts/verify.py`：**753 passed，261.96 秒**；包含 JS 語法、lint、format、型別、依賴、死碼、匯入邊界與測試。
- 執行範圍是鎖定 Python 3.13.7 的隔離 Linux 容器，4 CPU／4 GB；不是 Windows 實機或真實供應商驗收。
- 本機另有 macOS socket 綁定／重啟／HTTP 測試；sandbox 內 bind 曾被禁止，移至允許 loopback 的環境重跑 6 項通過。不是 Windows 證據。
- 初審 `1a6bcd1c-5b98-4c78-8113-830ea62eed18` 發現 S-001：新平手語義會扭曲舊預測 Brier。
- 複審 `bb37bb13-d702-49ad-b014-7ef0b9f472b1` 確认 S-001 已關閉，`closure-complete=true`、`prior-blockers-open=false`，兩項 deterministic evidence 通過。
- 報告同時保留 `no-new-findings=false`，**不要改寫成「零 findings」或「所有使用者回報已關閉」**。
- 更早一次 `adf9a01d-01e9-4d21-b2bf-7d8eefcda47e` 因審查期間候選變動而中止，不能當成通過證據；上面兩輪才是有效的初審／複審。
- 本次分批 commit 沒有修改受審查的程式內容。新增交接文件沒有另開程式審查，也沒有因此重跑整套測試。

本機證據（不在 clone 內容內，轉交另一台電腦前需另行安全保存）：

- `/Users/leo/.goldband/workflow-runs/artifacts/bb37bb13-d702-49ad-b014-7ef0b9f472b1-code.md`
- 同目錄 `bb37bb13-d702-49ad-b014-7ef0b9f472b1-review-closure.json`
- 同目錄 `1a6bcd1c-5b98-4c78-8113-830ea62eed18-bea80021-review-evidence.json`

### 效能證據

| 功能 | 使用者回報（尚未拿到原腳本重播） | 規格預算 |
| --- | --- | --- |
| 本週勝率 | 17–22 秒，預設會超時 | 2 秒 |
| 換人建議 | 估計超過 1 小時 | 15 秒 |
| 今日頁 | 12 分鐘仍未結束 | 1 秒 |
| 全聯盟 1 換 1 | 約 6 CPU 小時 | 30 秒 |

本機另用簡化的 14 隊 × 13 人、10 先發格、150 自由球員、預設 1,000 次模擬重現：3.81 秒後 `CalculationTimeout`。
這份 fixture 使用 PG/C 與可容納兩者的先發格，不是真實 NBA 隊伍／Yahoo 位置結構，不可替代使用者案例或正式 p50/p95。
附加 profiler 的一次執行約 5.01 秒，超時前約 5,800 次完整抽樣計分，基準目標約 16,200 次；有反覆計算相同抽樣均值的成本。
暫存腳本 `/private/tmp/fba-scale-audit.py` 與 profile `/private/tmp/fba-scale-audit.prof` 可能被清除，不是持久測試資產；接手後先取得原始案例並建立可重播 benchmark。

**Goldband provider timeout 已在另一個 repo 延長至 1,200 秒，與產品的 1／2／15／30 秒預算無關。**
Goldband repo `/Users/leo/goldband` 的 `9f33793` 是先前獨立提交；本次沒有修改或提交該 repo。

## 4. 尚未完成的工作清單

狀態用語：「確認」指本機程式路徑或重現；「待原例重播」指使用者提供的特定結果尚未獨立驗證。
以下所有項目都仍開放，不能因第一批相關修正而自動結案。

表內程式位置若省略根目錄，Python 模組均相對於 `src/fba/`；測試相對於專案根目錄。

### A. 資料入口、同步與儲存

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| A1 | 確認：只有自訂 `PlayerSnapshot` JSON，沒有現成 NBA 供應商轉接器。F1–F6、球員對照不能正常初始化。 | `data/inseason_sources.py`、`inseason/session.py`、來源設定 UI | 選定來源，實作真實 wire adapter、認證、分頁、限流、球員／逐場數據／賽程／傷病及回歸日；以授權實際回應從 clone 完成 F1/F2，再串 Yahoo。明列來源沒有提供的欄位。 |
| A2 | Yahoo `ST` 已修，但其他真實設定／比分結構未驗收。fixture 是合成資料。 | `data/yahoo*.py`、`tests/fixtures/yahoo-example.json` | 使用去識別且不含 token 的實際錄製回應，涵蓋設定、名單、對戰、自由球員、交易與比分；完成真實授權、設定確認及首次同步。 |
| A3 | 確認：沒有抓 Yahoo percent owned，丟人判斷缺來源。 | `data/yahoo.py`、`yahoo_normalize.py`、`inseason/today.py` | 抓取並正規化持有率與資料時間；缺值不可假裝為 0，畫面能辨認未知。 |
| A4 | 確認：每次同步全週比分與全部交易；季中超過 100 次請求的特定案例待重播。 | `data/yahoo.py`、`inseason/session.py` | 增量同步、分頁與舊比分更正策略；測試季中／季末及大量交易，14 隊完整同步 ≤60 秒、≤100 次請求，必要失敗清楚顯示。 |
| A5 | 確認：追加式快照／索引持續成長；bootstrap 讀取全部 sync-log。 | `data/storage.py`、`inseason/operations.py::bootstrap` | 有界查詢／分頁與明確保存策略，量測長季資料量。不得直接刪除原始快照、帳本或歷史預測來通過效能。 |
| A6 | 確認：Yahoo key →內部 ID 主要靠手動，跨季 key 改變。 | `contracts/yahoo.py`、`data/yahoo_normalize.py`、`session.map_player`、UI | 以可靠識別欄位做唯一匹配，歧義要求確認；顯示姓名／球隊等識別資訊。名單球員 100% 對照、自由球員未對照可見，換季不用逐人重輸。使用者估計約 190 名單＋400 自由球員。 |
| A7 | 確認：公開排名無可替換 adapter；交易中缺單一球員排名會報錯。 | `inseason/trades.py::evaluate_trade`、`contracts/inseason.py::SeasonPlayer` | 選定合法來源並實作可替換 adapter；明訂缺排名的可用性／呈現，避免一名球員令全部搜尋無結果，也不得捏造排名或接受機率。 |

### B. 效能與排陣／搜尋品質

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| B1 | 確認：每日列舉大量合法子集，每個候選以 Python 重算目標；本機已超時。 | `core/lineups.py`、`inseason/matchup.py`、`formulas/categories.py` | 先 profile；消除重複工作、適當批次化與重用合法組合。保留 ≤15 人與窮舉一致的正確性證據，不能只縮小搜尋或放寬 timeout。 |
| B2 | 確認：整週只有逐日反覆改善，非聯合全域最佳。使用者 30 例中 13 例輸、最差 0.016 對 0.240 待原例重播。 | `matchup.py::optimize_total` | 取得反例，比較整週排陣品質及規格要求的每日精確解；所有 F3/F4/F5 共用修正後結果。若用近似策略，須清楚說明並有品質證據，不能稱全域最優。 |
| B3 | 確認：剩餘賽季沒有使用既有 `season_simulations` 的較小抽樣量，也沒有跨請求保存至下次同步的快取。 | `inseason/season.py`、`Simulation`、`session.simulation` | 使用明確的 ROS 抽樣契約與共同亂數；快取按同步、聯盟、參數、帳本、名單／生效日等正確失效。抽樣數不同不能直接共用不相容的 draws。 |
| B4 | 確認：交易先完整計算 effects 才過濾，不是有效的計算前剪枝。 | `inseason/trades.py::search_trade_bundles` | 可驗證的上界／候選篩選；小聯盟多換多前十名與完整窮舉一致，記錄真正省略的昂貴計算數，不只統計最後呼叫 `evaluate_trade` 的次數。 |
| B5 | F3 換人、F4 全聯盟交易、F5 今日頁仍昂貴；使用者長時間數字未原樣重播。 | `recommendations.py`、`trades.py`、`today.py` | 使用者 14×13／10 starters／150 FA 案例與真實聯盟 p50/p95；分別達 15／30／1 秒。今日頁內丟人估值、替換人與整季評估也要納入。指定一隊 2 換 2 也要 ≤30 秒。 |
| B6 | 所有其他性能條款尚缺實際 2026–27 聯盟報告。 | F1/F2、同步、所有計算入口 | 500 人有效預測 ≤1 秒、球隊頁 ≤300ms、手調至更新勝率 ≤2 秒；含冷／熱資料與硬體資訊。產品 budgets 保持明確，不用延長 Goldband 時間宣稱改善。 |

### C. 預測、模擬與校準正確性

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| M1 | 確認：產出旗標仍取各場比率平均及其 empirical error，會受分鐘差異與零變異影響。 | `projection.py::player_flags`、`formulas` | 定義正確的總量／曝光量估計與抽樣誤差，納入不同上場分鐘、零計數與雜訊案例；建議乘數同源，公式登錄並有獨立數值答案。門檻須實證驗證。 |
| M2 | 多群組錯誤已修，預設群組仍粗；分鐘重分配只補了 q=0 排除。 | `projection.py::fallback_prior`、`adjustments.py::redistribute`、參數檔 | 位置／分鐘 peer 分組與資料依據、無 peer 明確失敗、雙位置去重；補部分傷病與隊伍分鐘分配的情境驗收。不要把粗群組描述成已校準先驗。 |
| M3 | 確認：同步後已結束的比賽可能落在 Yahoo actual 與 future draws 之間消失。 | `matchup.py::actual/total/daily_draws` | 核對 `through` 與來源比分的真正語義；覆蓋已完成、進行中、當天多場、官方更正。不漏算、不重複加入部分已含的比分，不猜歷史 Yahoo 先發。 |
| M4 | 確認：`game_draw` 在沒有自己的逐場 history 時報錯。 | `matchup.py::game_draw`、來源歷史資料契約 | 開季、新秀／新升上來球員可計算；補足歷史或明確定義先驗預測抽樣與不確定性。不能用全零／確定常數偷偷代替未知，也不能把未來資料帶入回測。 |
| M5 | 確認：一般 roster 路徑只用 active players，IL 未來模擬不足。 | `matchup.py::roster`、`season.py`、`today.py` | 包含預計回歸、移出 IL、容量與必要丟人等合法狀態轉換；避免球星 ROS 變零或被錯誤建議丟棄。第一批只修 `return_on`，未完成整個 IL 流程。 |
| M6 | 確認：類別與整週仍共用 c=0.8；匯出校準資料只有類別。 | 參數契約／defaults、`matchup.py`、`review.py::calibration_history/refit_history`、backtest | 分開類別 0.82／整週 0.80 的規格初值及出處；匯出兩層級的預測與結果，分開擬合、展示與樣本外驗證。舊版積分不可當新整週勝率訓練資料。 |
| M7 | 接受模型的新求解／分離檢查已測，但使用者原始 40 組資料、舊報告、實際提案流程未重播。 | `formulas/fitting.py`、`operations.py::refit_acceptance`、保存的 acceptance-fits | 重播原始正常與分離案例，驗證係數／需求方向、殘差、log loss、顯示與既有報告處理。不要把合成訓練擬合說成真實接受機率校準。 |

### D. 回顧與回測

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| R1 | 確認：開週頁會新增 prediction，回顧遍歷所有紀錄，指標被瀏覽次數加權。使用者 5 次→45 類別筆數待原例重播。 | `operations.py::week_result`、`review.py::weekly_review/cumulative_review` | 定義固定評估 cohort／去重鍵；同一資訊重開頁不改變評估權重。原始歷史不可直接刪除；需保留真正不同決策時間的可追溯性。 |
| R2 | 確認：沒有開過的週缺 prediction。 | `session.py` 同步完成路徑、`operations.py::complete_reviews` | 不依賴頁面瀏覽的當時預測保存；同步、排程、重啟不重複寫入。不准用事後資料補造事前預測。 |
| R3 | 確認：只要單一分箱偏差超門檻就提醒，稀疏資料容易一直亮。 | `review.py::calibration_bins/weekly_review/cumulative_review` | 明確樣本量／不確定性政策與參數出處，覆蓋少量與足量案例；不是直接提高門檻或隱藏提醒。 |
| R4 | 確認：現有 backtest 的對戰校準接受外部 predicted/observed，不能單憑它證明實際模擬器校準。 | `inseason/backtest.py`、`contracts/inseason_backtest.py`、`inseason/replay.py` | 用真實凍結時間序列跑實際引擎，前季訓練／後季 holdout；5/10/20/30/40 場預測誤差、旗標命中率、出賽 Brier、10 分箱≤5pp、規格 Brier 基準。換人 recall≥95%、與不動／公開排名策略比較也須真實因果重播。 |

### E. 畫面、公式與參數

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| U1 | F3 頁首缺對手、已過天數、剩餘場次／加人、整週未校準值與「什麼都不動」基準。類別表已有 raw probability，勿誤稱完全沒有原始機率。 | `apps/inseason/static/views.js`、結果契約 | 明確基準線與所選週資料，不和「無手調」基準混淆；數字可追溯且經瀏覽器核對。 |
| U2 | 缺完整「需要你確認」匯總清單；F1/F2 目前已有部分 flags／欄位。 | UI、projection flags、同步設定差異 | 依規格整理待確認事項、來源／理由／操作，核對忽略／到期／狀態改變後行為。 |
| U3 | 使用者回報球員卡命中數／算式與真正運算值不一致，待對指定數字重播。 | `views.js::playerCard`、`projection.py::blend_player/effective_projection` | 清楚分開先驗、混合、手調及最後由命中率重算的 made；抽 20 個 UI 數字獨立手算（至少 3 位小數）並驗證傳給模擬的值。 |
| U4 | 日期修正與交易內容欄位已改，缺新的完整瀏覽器驗收。 | today／trade／review UI | 聯盟美東日期、使用者台北顯示、DST、跨午夜、交易內容及舊版 Brier 標籤都實際操作讀回。 |
| F1 | 公式完整盤點未完成。原回報「只檢查 scalar」不完全符合目前程式：`test_formula_traces.py` 已同時檢查 scalar 與 vector；仍未涵蓋所有其他模組。 | `formulas/*`、`projection.py`、`matchup.py`、相關 tests | 盤點使用者所稱約 80 個未登錄函式的實際清單，分清公式與協調／驗證函式；建立全範圍一對一檢查，移出業務模組公式本體，UI trace 與實作同源。不得只改檢查範圍假裝覆蓋。 |
| F2 | 參數普遍標 Assumption，未建立 2025–26 原始回測證據對應。 | defaults/parameters.json、回測報告 | 逐鍵核對初值、來源、訓練／驗證季與限制；規格提到 `claude-report-v2/evidence-2026-09-27/backtest_inseason.py`、`backtest_matchup.py` 及 JSON，目前未取得。無證據不能改標已校準；OREB 等未回測項另列。 |
| F3 | 換聯盟 fixture 改了年份／隊數／scoring，類別與位置沒有充分不同。 | `tests/inseason_support.py`、`test_inseason_portability.py`、跨功能測試 | 真正改類別公式（含比率／A/T）、位置、10 隊、H2H Each Category，F0–F6 跑完且合法；只改設定，不在程式塞聯盟特例。 |

### F. 交付與實際環境

| ID | 未完成的驗收 | 完成條件 |
| --- | --- | --- |
| V1 | 真實 Yahoo OAuth、refresh、撤銷／失效、限流、設定差異與資料時效 | 使用授權測試帳號，安全保存錄製 fixture；驗證只讀、來源時間、失敗訊息及恢復。合成資料或 mock token 不算。 |
| V2 | Windows 原生執行 | 真實 Windows 11 驗證 socket 互斥／保留 port、10013 換 port、單一實例、同時雙開、正常重啟、強制終止後 5 秒內無殘留、寫入中斷復原；共用 runtime 也要驗證競標桌與季賽助手共存。 |
| V3 | 乾淨 clone 啟動 | macOS／Windows 無 Python 環境依 README 安裝必要的 uv、由 uv 取得鎖定 Python 後雙擊啟動；中文／空白路徑、自己的憑證、首次資料取得／同步。先前 macOS 副本測試機已有 Python 3.13.7，不算無 Python 證據。 |
| V4 | 卡住的既有實例提示是否可見 | clone 命令檔實際雙擊第二次時可見診斷，不偷開另一實例。原「打包版沒有結束選單」已被 clone 交付取代；仍要驗證現有「結束助手」、關分頁及命令視窗的行為。 |
| V5 | 三平台 CI 與數值一致性 | 真正執行 macOS／Windows／Linux 完整工作流程；同快照、帳本、參數與 seed，排序相同、數值差≤1e-9。CI YAML 存在、單機 golden fixture 更新都不算跨平台通過。 |
| V6 | 整份規格的最後整合驗收 | 逐條重查原規格，尤其跨功能手調／撤銷、因果性、錯誤可見、秘密掃描、公式手算、設定可替換、來源時間與日誌；附真實環境證據、未測項與實際 p50/p95，再開獨立審查。 |

## 5. 需要使用者提供／決定的資料

以下問題已提出，交接時尚未取得回答；**不得把沒有回答視為授權**。

1. 原缺陷重現腳本、合成聯盟快照、30 個排陣案例、40 組接受模型資料、原效能報告的位置。
2. NBA 供應商方向：已有供應商、採用 BALLDONTLIE，或只接受免費來源。
   - 2026-09-30 核對的 [BALLDONTLIE 官方文件](https://nba.balldontlie.io/#account-tiers) 顯示逐場 stats／傷病需要 ALL-STAR（當時 US$9.99/月）或以上及 API key；費率與欄位在實作前重查。
   - [官方條款](https://www.balldontlie.io/terms.html) 是來源評估依據之一，不代表已實際串接，也不是 NBA 官方 feed。尚未訂閱、沒有金鑰、沒有發出付費呼叫。
   - 帳號／付款由使用者處理；密鑰經正式本機憑證路徑提供，勿要求貼入 repo、日誌或交接文件。
3. 真實 Yahoo 存取資格／錄製回應，及規格引用的 2025–26 原回測程式與 JSON。

不依賴這些輸入的確定性修復可以繼續；不要把全部未完成工作都歸因於使用者未提供資料。

## 6. 建議接續順序

1. 保存／取得可重現案例，核對遠端與工作樹；按使用者最新來源選擇完成 A1/A2/A6。先打通真實資料，不再增加需要人自行製造的專案 JSON 入口。
2. 修 M1/M3/M4/M5/M6，確立模擬資料及狀態語義。建立能抓出已完賽遺漏、IL、零歷史與校準錯配的獨立答案。
3. 以固定案例處理 B1–B5，保留正確性 oracle；先消除重複計算、實作 ROS 低抽樣／快取，再改善整週搜尋與真正剪枝。分開量測品質與速度。
4. 補 A3–A7、R1–R4、U1–U4、F1–F3；自動保存預測與回顧評估 cohort 一起設計，避免只做去重卻丟失真實決策紀錄。
5. 執行 V1–V6。回測是交付門檻，不重新變成執行時功能鎖。
6. 每批驗證與獨立審查後分批 commit；push 另外取得授權。

## 7. 接手操作

```sh
cd /Users/leo/fantasy-basketball-assistant
git status --short
git log -5 --oneline
# 已有鎖定環境時執行；不要把重新安裝套件當作修復。
uv run --no-sync python scripts/verify.py
```

需要初始化開發環境時依 README 使用 `uv sync --locked`；完整驗證另需 Node.js 與 C++ 編譯器。
實際使用者 clone 啟動方式見 [季賽助手說明](inseason.md)，不要把開發工具要求混入一般產品流程。

Goldband 使用 `$goldband review code`，先讀安裝的 skill 與 `.workflow-launcher.json`，不自行猜其他呼叫方式。
之前有效的證據 manifest 位於 `/private/tmp/fba-clone-review/contract.json`；這是本機暫存資產，接手須核對是否仍存在、image／lock hash 是否適用。
正式 closure 的 `--closure-artifact` 使用初審 evidence JSON，不是 closure JSON；審查期間固定候選，修改後重新建立相符證據。
不要繞過 sandbox、auto-review、pre-commit 或 Goldband lineage。缺真實平台證據就明說，不將 gate 綠燈冒充完整交付。
