# 公式所有權盤點

本文件接續 F1，涵蓋競標桌與季賽助手。**F1 尚未結案。**
`formula-ownership.json` 是待維護的語意盤點，不是驗收報告；原始測試、瀏覽器與審查證據放在 Git 外。

## 本批抽出的公式

前批登錄表由 90 條增至 114 條。以下 24 條已具有唯一登錄 ID、實作、輸入單位、LaTeX、獨立預期答案與可重播代入值。業務呼叫處沿用原有亂數、樣本數、搜尋規則和加總順序。

| 用途 | 登錄 ID | 實作 owner |
| --- | --- | --- |
| 模擬樣本摘要 | `sample_mean`, `sample_variance`, `sample_deviation` | `formulas/simulation.py` |
| 類別及衍生數據 | `linear_totals`, `category_ratio`, `game_threshold`, `comparison_margin` | 同上 |
| 排陣保守界限 | `linear_interval`, `ratio_interval` | 同上 |
| 已入帳戰績與模擬晉級 | `standings_credit`, `playoff_odds` | 同上 |
| 健康路徑與排陣價值 | `health_decay`, `control_covariance`, `scheduled_health`, `ranked_health_value` | 同上 |
| 抽樣變換 | `lognormal_taste`, `normal_quantile` | 同上 |
| 組隊成本界限 | `portfolio_cost_floor` | 同上 |
| 對手出價 | `highest_bid_survival`, `median_bid` | 同上 |
| 成交與公開估值 | `second_price`, `anchor_scale` | `formulas/scalar.py` |
| 球隊分鐘／賽季貢獻 | `minute_budget`, `season_rate` | 同上 |

分鐘、球隊使用量、歷史來源平均、衍生雙十、歷史對戰計分亦重用既有的 `linear`、`product`、`ratio`、`threshold`、`category_points`。Scalar 的 `mean` 使用 `math.fsum`；樣本版 `sample_mean` 保留 NumPy 的軸與加總語義。兩者輸入契約及數值實作不同，不能在這次搬移時換掉加總方式。抽樣 RNG、索引、日期、集合、容量與搜尋本身不是獨立預測公式。

公式目錄依向量、模擬與競標領域分檔，公開登錄 ID、順序、實作、例子與單位保持相同。此拆分符合既有提交 hook 的 600 行上限。

## 2026-10-02 接續抽取

新增 `management_gain`、`availability_probability`、`upper_total`、`health_step`、`subset_bound`、`outward_bound`、`positive_part`、`absolute_error` 共 8 條。保守界限保留 `fsum`、逐步 outward rounding 和無界內部 fallback；沒有建立堆疊上下界的大型中間陣列。角色／產出／手調旗標、校準偏差與競標 cap 損失保留登錄 trace，歷史報告與來源換算重用既有公式。新候選的測試與獨立審查以 Git 外報告為準，沒有沿用前批綠燈。

這批已通過完整 1,169 項測試與獨立審查 `3b0a7af3-a147-4076-964e-96e9de4325d0`，分為 `a2aff0b`、`a1ddd86`、`4b676ee` 三批本機提交，未推送。接續候選另新增 `historical_games`、`average_surplus`、`array_product`、`nonnegative_samples`、`derived_sum`、`nested_count_limit`、`strict_win` 七條，登錄總數 129；這批已通過完整 1,176 項測試與審查 `b967c110-58f5-43ce-8c59-da5a33aadb6e`，0 findings、必要執行證據完整；提交為 `f2dfbc8`、`818633e`、`86a32fc`，全部未推送。布林健康矩陣、availability mask、made ≤ attempted、GP 範圍、剩餘現金平均與 strict-win 使用同一 owner。衍生數據保留原 NumPy 加總順序，沒有替換 RNG、樣本數或預算。

Today 的類別影響現在保留「raw 機率差 → 類別校準乘數」兩段 trace；在合成資料的實際 Chrome 展開後，`0.76 − 0.675 = 0.085`、`0.085 × 0.82 = 0.0697` 與顯示的 `PTS Δ 0.07` 相符。這只是新增呈現的整合證據，不替代全產品隨機 20 個畫面數字驗收。

## 全範圍盤點與自動檢查的界限

`tests/formula_ownership_support.py` 每次掃描全部 `src/fba/**/*.py`，包含 module／class 內容、method、nested function、lambda 內的算術及數值正負號；排除型別註記與位元旗標運算。它只找候選，沒有宣稱能從 AST 自動判斷業務語意。

目前 142 個 Python 檔有 464 個候選 owner：129 `registered`、1 `component`、130 `composition`、17 `validation`、187 `structure`、0 `pending`。數字不是公式總數。Paths、字串、集合、日期與 solver constraint arithmetic 也會被掃到。本輪讀完剩餘 87 個函式本體，抽取業務算式後逐條記錄具體分類理由。合法名單／現金保留、搜尋順序與停止條件不當成預測公式；使用既有登錄公式的編排保留在引擎。

另逐檔核對 1 個 C++ 與 10 個 JavaScript 來源，保存完整檔案 SHA-256、分類、理由與所用登錄 ID。測試檢查新增／刪除／改寫來源時必須更新這份人工盤點，不能自動判斷語意。拍賣頁的價差／折扣、cap 對市場價的空間、全場現金／名額與買／不買餘額已移回後端，使用既有 `difference`、`ratio`、`linear`。重點價差門檻沿用 5／0.2，改由 `model.market.focus_difference`／`focus_discount` 設定；舊模型保持相同行為，範例與受影響 schema 同步。Python／C++／JavaScript 的 pending 均為 0；這批新候選仍需完整測試／獨立審查。

盤點也涵蓋目前使用的 NumPy `minimum`、`maximum`、`add`、`logaddexp`、`count_nonzero`；五個不帶算術運算子的案例先重現漏記，再修正檢查。新增六個候選逐條核對，其中五個只是 `set.add` 的保守誤報，一個是 native 日期容量驗證。呼叫名稱檢查會有誤報，且不是型別推導或數值語意證明。

- 每個候選保留完整函式內容的 AST signature（保留外層運算、分支與語句順序；忽略空白、註解與行號）、分類、owner 與分類理由。`pending` 保留原函式為待確認 owner，沒有當成例外放行或已審查。
- CI 檢查新增／刪除／搬移／改寫候選時必須更新盤點，禁止僅更新 signature 而略過語意核對。測試不會自動產生或更新文件。
- CI 從兩份執行登錄表比對全範圍 `registered` owner，檢查 ID 和 implementation 各自一對一；公式必須在 `fba.formulas`。私有 quadrature integrand 明確歸屬 `market_normalization`。
- 手算範例測試獨立提供答案，並核對 immutable inputs／重播。不從受測實作產生 expected。
- **上述檢查通過只證明在所列語法／呼叫名稱範圍內沒有未記錄的變動及已登錄公式的一對一；不代表盤點分類自動正確，也不代表所有產品數字都有完整 UI trace。Python 沒有 pending 不等於 F1 通過。**

## F1 仍須完成

1. 拍賣呈現搬移需經本批完整驗證與獨立審查；沒有 pending 只代表人工盤點完成，不取代語意審查。
2. `src/fba/native/season.cpp` 的 `Simulation::choose` 現由 `adapters/native_formula.py` 把唯一登錄的 `management_gain` 表達式轉成 C++ header；候選上界也用同一函式。白名單 AST 遇到未支援語法明確失敗，沒有 eval 或第二份算式。native artifact 雜湊同時綁定 template 與產生的 header；ABI、樣本與日期×球員配置保持原樣。另有實際編譯 C++ 的獨立答案及 provenance 失配測試；每週排序權重亦由同一轉譯器產生既有 `product` 的 C++ 函式；其餘 native 搜尋／排序算術已逐檔核對並記錄具體理由。
3. C++／JS 人工盤點與檔案變動 gate 已補；百分比／貨幣格式、日期／計時與索引是呈現或結構運算，預測／市場算式由後端 owner 提供。仍需確認所有最終功能的數字都有足夠 trace，不能把逐檔分類當成 UI 全面驗收。
4. 已登錄內部公式仍須追到最終 UI 的代入值。保守區間在內部允許無界 `±inf`；公開的 `evaluate_array` 明確拒絕 non-finite evidence，不把無界值裝成可展示的數字。
5. 原始重構規格的共用公式要求依本文件同步補充；本機 Chrome 已完成兩應用分層隨機 20 個 scalar trace 結果的獨立手算與逐一展開，見下節。129 條獨立範例及 20 個畫面數字都不替代尚未逐功能完成的全畫面 trace 核對。

`playoff_odds` 保留既有 seed tie-break 模型假設；本次抽取不是已核實 Yahoo 最終排名規則、真實晉級機率或歷史校準的證據。效能工作依目前使用者決定暫停，本次不宣稱任何預算達標。

## 重構規格同步補充與畫面驗收

本節同步原「Fantasy 籃球助手重構規格與驗收條件」的競標公式要求，與季賽規格第 2 節共用公式層一起適用：兩應用不互相 import，scalar／array 的 ID、LaTeX、單位、參數引用與例子由同一登錄表產生；native 只能從白名單 owner 產生相符函式。引擎可做編排、合法性驗證、搜尋與排序；畫面只呈現後端值與共同 trace，不重算業務數字。

新增市場呈現結果包含 gap／discount／focus、cap edge、房間 cash／spendable／slots 與買／不買餘額；來源缺值維持空值。舊執行紀錄可讀，不補造當時 trace；新計算的房間摘要缺失時畫面明確失敗。既有比較效用的發布四捨五入與最佳化未改，trace 保留未四捨五入的實際代入值。

Git 外 `random-20-handcheck.json` 保存 seed 20261002、拍賣 66／Today 111 筆實際 DOM scalar trace 母體、各抽 10 筆、獨立 Decimal／標準數學函式的答案與誤差。未 import 產品公式產生 expected。20 筆結果誤差均 ≤1e-12，保存三位小數答案，且在 Chrome 實際展開後保存文字／截圖。另核對 `50−4=46`、`50−2=48`、`15−13=2` 與全場 `[50,50] → 100`、`[2,2] → 4`。這是合成 fixture 的正式程式／API／共同 renderer 證據，不是 live provider、隨機模型校準或三平台驗收。
