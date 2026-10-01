# 公式所有權盤點

本文件接續 F1，涵蓋競標桌與季賽助手。**F1 尚未結案。**
`formula-ownership.json` 是待維護的語意盤點，不是驗收報告；原始測試、瀏覽器與審查證據放在 Git 外。

## 本批抽出的公式

登錄表由原先 90 條增至 114 條。以下 24 條已具有唯一登錄 ID、實作、輸入單位、LaTeX、獨立預期答案與可重播代入值。業務呼叫處沿用原有亂數、樣本數、搜尋規則和加總順序。

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

## 全範圍盤點與自動檢查的界限

`tests/formula_ownership_support.py` 每次掃描全部 `src/fba/**/*.py`，包含 module／class 內容、method、nested function、lambda 內的算術及數值正負號；排除型別註記與位元旗標運算。它只找候選，沒有宣稱能從 AST 自動判斷業務語意。

目前 140 個 Python 檔有 449 個候選 owner：114 `registered`、1 `component`、34 `composition`、17 `validation`、176 `structure`、107 `pending`。數字不是漏登錄公式數。Paths、字串、集合、日期與 solver constraint arithmetic 也會被掃到。
本輪逐條核對並分類了原先 188 個 pending 中的 81 個；剩餘 107 個仍待語意處理。分類理由寫在每一筆 JSON，不將含有健康比例、市場餘額、統計誤差或搜尋上界的混合函式當成單純索引／排序放行。

盤點也涵蓋目前使用的 NumPy `minimum`、`maximum`、`add`、`logaddexp`、`count_nonzero`；五個不帶算術運算子的案例先重現漏記，再修正檢查。新增六個候選逐條核對，其中五個只是 `set.add` 的保守誤報，一個是 native 日期容量驗證。呼叫名稱檢查會有誤報，且不是型別推導或數值語意證明。

- 每個候選保留完整函式內容的 AST signature（保留外層運算、分支與語句順序；忽略空白、註解與行號）、分類、owner 與分類理由。`pending` 保留原函式為待確認 owner，沒有當成例外放行或已審查。
- CI 檢查新增／刪除／搬移／改寫候選時必須更新盤點，禁止僅更新 signature 而略過語意核對。測試不會自動產生或更新文件。
- CI 從兩份執行登錄表比對全範圍 `registered` owner，檢查 ID 和 implementation 各自一對一；公式必須在 `fba.formulas`。私有 quadrature integrand 明確歸屬 `market_normalization`。
- 手算範例測試獨立提供答案，並核對 immutable inputs／重播。不從受測實作產生 expected。
- **上述檢查通過只證明在所列語法／呼叫名稱範圍內沒有未記錄的變動及已登錄公式的一對一；不代表 107 個 pending 已完成語意分類，也不代表所有產品數字都有完整 UI trace。**

## F1 仍須完成

1. 逐條審閱 JSON 中的 `pending`；業務算式搬到既有公式 owner，協調／搜尋／驗證須寫具體理由。優先看 `auction/managed.py` 的 availability／control composition、`formulas/valuation.py` 的類別影響、`formulas/market.py` 的市場餘額及 `inseason` 的報告／界限／排序計算。
2. `src/fba/native/season.cpp` 的 `Simulation::choose` 仍直接計算短期增益與長期機會成本，尚未和公式登錄形成唯一實作。不能用 Python inventory 宣稱覆蓋 native；也不能為抽取而新增樣本×日期×球員平方的巨大 gain table。需要維持現有 native ABI／資源界限的方案和等價 oracle。
3. `src/fba/apps/**/*.js` 尚需完成各顯示數字／unit conversion／日期算術與後端 trace 的逐項核對；Python gate 不涵蓋 JavaScript。
4. 已登錄內部公式仍須追到最終 UI 的代入值。保守區間在內部允許無界 `±inf`；公開的 `evaluate_array` 明確拒絕 non-finite evidence，不把無界值裝成可展示的數字。
5. 重構規格同步及隨機 20 個畫面數字手算仍需逐條原始證據。114 條範例不能替代這項整合驗收。

`playoff_odds` 保留既有 seed tie-break 模型假設；本次抽取不是已核實 Yahoo 最終排名規則、真實晉級機率或歷史校準的證據。效能工作依目前使用者決定暫停，本次不宣稱任何預算達標。
