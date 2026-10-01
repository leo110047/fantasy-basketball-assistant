# 公式所有權盤點

本文件接續交接清單 E/F1，涵蓋競標桌與季賽助手。**公式所有權與 UI trace 的本機驗收已完成。**
原規格功能 F1「有效預測」的跨季校準、真實來源與效能是其他驗收條款，仍未完成。
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

新增 `management_gain`、`availability_probability`、`upper_total`、`health_step`、`subset_bound`、`outward_bound`、`positive_part`、`absolute_error` 共 8 條。保守界限保留 `fsum`、逐步 outward rounding 和無界內部 fallback；沒有建立堆疊上下界的大型中間陣列。角色／產出／手調旗標、校準偏差與競標 cap 損失保留登錄 trace，歷史報告與來源換算重用既有公式。各批候選使用各自的完整測試與獨立審查，原始證據以 Git 外報告為準。

這批已通過完整 1,169 項測試與獨立審查 `3b0a7af3-a147-4076-964e-96e9de4325d0`，分為 `a2aff0b`、`a1ddd86`、`4b676ee` 三批本機提交，未推送。接續候選另新增 `historical_games`、`average_surplus`、`array_product`、`nonnegative_samples`、`derived_sum`、`nested_count_limit`、`strict_win` 七條，登錄總數 129；這批已通過完整 1,176 項測試與審查 `b967c110-58f5-43ce-8c59-da5a33aadb6e`，0 findings、必要執行證據完整；提交為 `f2dfbc8`、`818633e`、`86a32fc`，全部未推送。布林健康矩陣、availability mask、made ≤ attempted、GP 範圍、剩餘現金平均與 strict-win 使用同一 owner。衍生數據保留原 NumPy 加總順序，沒有替換 RNG、樣本數或預算。

Today 的類別影響現在保留「raw 機率差 → 類別校準乘數」兩段 trace；在合成資料的實際 Chrome 展開後，`0.76 − 0.675 = 0.085`、`0.085 × 0.82 = 0.0697` 與顯示的 `PTS Δ 0.07` 相符。這只是新增呈現的整合證據，不替代全產品隨機 20 個畫面數字驗收。

## 全範圍盤點與自動檢查的界限

`tests/formula_ownership_support.py` 每次掃描全部 `src/fba/**/*.py`，包含 module／class 內容、method、nested function、lambda 內的算術及數值正負號；排除型別註記與位元旗標運算。它只找候選，沒有宣稱能從 AST 自動判斷業務語意。

目前 142 個 Python 檔有 464 個候選 owner：129 `registered`、1 `component`、130 `composition`、17 `validation`、187 `structure`、0 `pending`。數字不是公式總數。Paths、字串、集合、日期與 solver constraint arithmetic 也會被掃到。本輪讀完剩餘 87 個函式本體，抽取業務算式後逐條記錄具體分類理由。合法名單／現金保留、搜尋順序與停止條件不當成預測公式；使用既有登錄公式的編排保留在引擎。

另逐檔核對 1 個 C++ 與 10 個 JavaScript 來源，保存完整檔案 SHA-256、分類、理由與所用登錄 ID。測試檢查新增／刪除／改寫來源時必須更新這份人工盤點，不能自動判斷語意。拍賣頁的價差／折扣、cap 對市場價的空間、全場現金／名額與買／不買餘額已移回後端，使用既有 `difference`、`ratio`、`linear`。重點價差門檻沿用 5／0.2，改由 `model.market.focus_difference`／`focus_discount` 設定；舊模型保持相同行為，範例與受影響 schema 同步。Python／C++／JavaScript 的 pending 均為 0。拍賣候選完整通過 1,185 項測試（292.59 秒）與獨立審查 `2ad24675-08fc-48f9-92ec-73935d864e2a`，0 findings、兩項 required verified-pass；304 檔提交前 0 drift，分批提交 `ad00944`、`3980741`，未推送。

盤點也涵蓋目前使用的 NumPy `minimum`、`maximum`、`add`、`logaddexp`、`count_nonzero`；五個不帶算術運算子的案例先重現漏記，再修正檢查。新增六個候選逐條核對，其中五個只是 `set.add` 的保守誤報，一個是 native 日期容量驗證。呼叫名稱檢查會有誤報，且不是型別推導或數值語意證明。

- 每個候選保留完整函式內容的 AST signature（保留外層運算、分支與語句順序；忽略空白、註解與行號）、分類、owner 與分類理由。`pending` 保留原函式為待確認 owner，沒有當成例外放行或已審查。
- CI 檢查新增／刪除／搬移／改寫候選時必須更新盤點，禁止僅更新 signature 而略過語意核對。測試不會自動產生或更新文件。
- CI 從兩份執行登錄表比對全範圍 `registered` owner，檢查 ID 和 implementation 各自一對一；公式必須在 `fba.formulas`。私有 quadrature integrand 明確歸屬 `market_normalization`。
- 手算範例測試獨立提供答案，並核對 immutable inputs／重播。不從受測實作產生 expected。
- **上述檢查通過只證明在所列語法／呼叫名稱範圍內沒有未記錄的變動及已登錄公式的一對一；不代表盤點分類自動正確，也不代表所有產品數字都有完整 UI trace。Python 沒有 pending 不等於 F1 通過。**

## F1 完成項與界限

1. 拍賣呈現搬移已完成相符的完整驗證與獨立審查；後續季賽呈現亦已通過 1,193 項完整測試與獨立審查，見下節。沒有 pending 不取代語意審查。
2. `src/fba/native/season.cpp` 的 `Simulation::choose` 現由 `adapters/native_formula.py` 把唯一登錄的 `management_gain` 表達式轉成 C++ header；候選上界也用同一函式。白名單 AST 遇到未支援語法明確失敗，沒有 eval 或第二份算式。native artifact 雜湊同時綁定 template 與產生的 header；ABI、樣本與日期×球員配置保持原樣。另有實際編譯 C++ 的獨立答案及 provenance 失配測試；每週排序權重亦由同一轉譯器產生既有 `product` 的 C++ 函式；其餘 native 搜尋／排序算術已逐檔核對並記錄具體理由。
3. C++／JS 人工盤點與檔案變動 gate 已補；百分比／貨幣格式、日期／計時與索引是呈現或結構運算，預測／市場算式由後端 owner 提供。本輪再逐功能核對結果契約與實際 renderer，補四組 trace；逐檔分類本身不替代畫面證據。
4. 內部公式與可展示計算分開核對，最終 UI 的代入值見下方對照。保守區間在內部允許無界 `±inf`；公開的 `evaluate_array` 明確拒絕 non-finite evidence，不把無界值裝成可展示的數字。
5. 原始重構規格的共用公式要求依本文件同步補充；本機 Chrome 已完成兩應用分層隨機 20 個 scalar trace 結果的獨立手算與逐一展開，見下節。129 條獨立範例、20 個畫面數字與下方逐功能核對合併作為本機驗收證據；不外推為所有來源、歷史或平台已驗。

`playoff_odds` 保留既有 seed tie-break 模型假設；本次抽取不是已核實 Yahoo 最終排名規則、真實晉級機率或歷史校準的證據。效能工作依目前使用者決定暫停，本次不宣稱任何預算達標。

## 重構規格同步補充與畫面驗收

本節同步原「Fantasy 籃球助手重構規格與驗收條件」的競標公式要求，與季賽規格第 2 節共用公式層一起適用：兩應用不互相 import，scalar／array 的 ID、LaTeX、單位、參數引用與例子由同一登錄表產生；native 只能從白名單 owner 產生相符函式。引擎可做編排、合法性驗證、搜尋與排序；畫面只呈現後端值與共同 trace，不重算業務數字。

新增市場呈現結果包含 gap／discount／focus、cap edge、房間 cash／spendable／slots 與買／不買餘額；來源缺值維持空值。舊執行紀錄可讀，不補造當時 trace；新計算的房間摘要缺失時畫面明確失敗。既有比較效用的發布四捨五入與最佳化未改，trace 保留未四捨五入的實際代入值。

Git 外 `random-20-handcheck.json` 保存 seed 20261002、拍賣 66／Today 111 筆實際 DOM scalar trace 母體、各抽 10 筆、獨立 Decimal／標準數學函式的答案與誤差。未 import 產品公式產生 expected。20 筆結果誤差均 ≤1e-12，保存三位小數答案，且在 Chrome 實際展開後保存文字／截圖。另核對 `50−4=46`、`50−2=48`、`15−13=2` 與全場 `[50,50] → 100`、`[2,2] → 4`。這是合成 fixture 的正式程式／API／共同 renderer 證據，不是 live provider、隨機模型校準或三平台驗收。

## 最終功能數字追溯核對

本輪逐功能閱讀實際 renderer 與結果契約，確認來源事實、設定、計數、日期／格式不當成預測公式。發現每週我方／對手總量、分箱平均、手調乘數、接受模型 Log loss 四組已計算但缺乏展開證據；本候選補上同一 owner 的 trace，不新增公式 ID。補齊候選已通過完整驗證與獨立審查，見本機驗收結論；不因此宣稱全部 UI 狀態或完整規格通過。

| 最終呈現 | 公式與傳遞路徑 | 本機證據邊界 |
| --- | --- | --- |
| 球員卡混合／權重／最終數據 | `blend`, `minutes`, `weight`, `product`, `linear`, `expectation` → `EffectivePlayer.traces` → `playerCard` | 既有 20 數字、手調／撤銷整合證據；新增乘數保留逐筆 entry／target 的原值及係數 |
| 球隊分鐘與旗標 | `linear`, `minute_budget`, `difference`, `mean`, `exposure_rate`, `exposure_error`, `absolute_error` → team view／flag traces | 既有獨立公式答案；門檻仍未實證校準 |
| 每週／Today 類別總量與比率 | `nonnegative_samples`, `linear_totals`, `category_ratio` → `CategoryForecast.value_traces` → `forecastCard` | 新增只含我方／對手兩列平均值，與原計算共用 terms 及零分母政策；不保存完整抽樣陣列 |
| 每週機率／誤差與 Today 影響 | `calibration`, `z`, `normal`, `error`, `product`, `difference` → forecast／Today traces | 隨機 DOM 手算及實際展開；strict-win 語義保持原樣 |
| 換人與交易 | `difference`, `management_gain`, `rank_value`, `acceptance`, `product`, `playoff_odds` → plan／trade traces | 小型完整枚舉與既有合成 UI；大型 coordinate／shortlist 品質及真實 recall 仍未驗 |
| 交易接受模型報告 | `log_loss`, `mean`, `absolute_error` → `refit_acceptance`／bins → 共同 renderer | 新候選加入 Log loss 和分箱 trace；係數是數值求解結果，不能冒充樣本外機率 |
| 每週／累積回顧 | `brier`, `mae`, `mean`, `effective_samples`, `monitor_margin`, `absolute_error` → review traces | 新候選保留實際分箱成員平均及每週展開欄；舊紀錄不補造 trace |
| 拍賣公允價／成交／停損 | `standardize`, `dollar_value`, `market_*`, `affordable_cap`, `difference`, `ratio` → detail／market／cap traces | 已審查提交及隨機手算；來源欄位缺失維持未知 |
| 拍賣管理調整／組隊比較 | `management_gain`, `sample_mean`, `product`, `difference`, `linear` → diagnostics／comparison／room traces | native 由唯一 owner 產生；各領域保留原 reduction 順序與發布 precision |

手調覆寫與狀態是使用者／參數提供的值，保留原始紀錄；只有乘數計算產生新的數值 trace。分鐘線、校準圖直接繪製同一結果資料。前端不重做混合、市場、分箱或交易算式。

合成 Chrome 已逐一展開讀回：PTS 的 `[133.3668, 136.514]`、FG 比率 `[47.631,48.755]/[95.262,97.51]=[0.5,0.5]`、分箱六筆平均預測 `0.4672` 與實際 `0.5`、FGA 手調 `0.333333…×1.5=0.5`、36 筆均為 `p=0.5` 的提案 Log loss `ln(2)=0.6931471805599453`。原始文字／截圖在 Git 外 `category-evidence-*.txt`／`.jpg`。兩個驗收服務皆經畫面「結束助手」正常關閉；沒有呼叫供應商、讀 OS 憑證庫或執行 Yahoo 交易。此為正式程式的合成資料整合證據，不是 live 或歷史校準。

## 本機驗收結論

本輪逐條分類 464 個 Python 候選並核對 C++／JavaScript，129 條登錄有一對一、domain、獨立答案與 replay 檢查。跨競標桌／季賽助手隨機 20 個實際 DOM 數字完成手算，另補每週總量／比率、分箱、手調乘數及接受模型 loss 的正式程式合成 UI 整合，完整候選通過 1,193 項測試與 `8341a8ab-9167-4f54-86a8-5939de335d26` 獨立審查（candidate `85ee5773798d505c8403ea919b526097d7fc6a935615ba793643878a30c35467`）。E/F1 的本機範圍結案；自動盤點本身不宣稱能證明語意。

使用者已授權直接推至 main；`49e9bc9825235ba66ea66ac5b8eb308a920d268b` 已推送並以遠端 SHA 讀回。三平台 CI 正在執行，結果另記 Git 外最新狀態。這次後續只更新文件與驗收狀態，程式／設定／測試沒有更動；不沿用本機證據宣稱平台或供應商通過。
