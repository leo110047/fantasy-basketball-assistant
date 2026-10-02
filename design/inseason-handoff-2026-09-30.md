# 季賽助手交接：未完成工作與驗收缺口

更新日期：2026-10-02。專案：`/Users/leo/fantasy-basketball-assistant`。

**整份規格尚未完成。** 接手時已核對本機與遠端 main 均為 `8d72a3bdd9c41b8f1d5f7540891432c14480b98a`，本輪使用者確認 ScoreTape receipt 與 Yahoo 核准尚未取得。下列歷史平台實測另標候選；公式所有權／UI trace 本機驗收及三平台合成資料 CI 已通過。真實 NBA／公開排名來源、真實 Yahoo、因果歷史校準、搜尋品質與部分實際交付情境仍缺證據，效能工作依使用者指示暫停。

已移除完成用途的 Windows 暫時診斷，scripts policy 回歸檢查必要的三平台 verify 入口。後續修補多 IL 的完整合法釋出路徑比較，並保留以下未完成條件。提交序列以 `git log` 為準；最新本機審查／CI 證據入口是 `/private/tmp/fba-continuation-report/current-status.md`，暫存資產可能清除，並不隨 clone 交付。

## 1. 權威需求與已接受的決定

- 原規格：`/Users/leo/Downloads/季賽助手：功能規格與驗收標準.md`；共用層規格：`/Users/leo/Downloads/Fantasy 籃球助手重構規格與驗收條件.md`。原文件未改動。91 條驗收項已對照，0 條漏列，詳 Git 外 `spec-acceptance-readback.md`；列入追蹤不等於通過。
- 現行計算契約見 [計算與資料契約補充](inseason-remediation-2026-09-30.md)，公式盤點見 [公式所有權](formula-ownership.md)。本文件的 37 列追蹤完整缺陷與已知交付缺口。
- 交付方式為 **clone + 雙擊命令檔**，取代 installer／PyInstaller／DMG。使用者已授權繼續實作、分批 commit、直接推 main；部署、付費方案、代開帳號及 Yahoo 寫入未獲授權。Yahoo 維持唯讀，建議由使用者手動執行。
- Today 沿用有效且已保存的 F3／IL 計畫，不再逐名重跑 ROS 移除搜尋。一般週不得降低 ROS；只有輸球即淘汰或確定失去晉級機會、且贏仍有晉級路徑時才優先本週。F3 先比較對目前隊伍貢獻低的球員；使用者於本輪接受品質優先，之後補查其餘合法方案，逾時明確回報未完成。
- 自動交易先用公開排名比較整包價值，較低估值至少達較高估值的初值 70%，可保存調整 50–100%。缺排名明示未知，手動交易仍可看量化影響。通過門檻後保留完整搜尋；減少候選不等於 top-10 品質通過。
- 來源只接受免費且條款允許。使用者未提供原始缺陷案例，已自行建立合成驗證。沒有因果 holdout 證據不調整或改標模型參數為已校準。效能工作暫放，產品樣本、候選及 budgets 保持原契約。

## 2. 最新證據與適用範圍

- Goldband `29f5e7d0-4f84-43f6-853e-44766363595b`：固定候選 `7ff2a2191bc62315c0104e0fb0e42970da824b3787ae22ce35184333f21a22f2`，完整 1,199 passed／297.47 秒，兩項 required evidence fresh verified-pass，0 finding／gap／incomplete，completion-authorized=true；308 檔提交前 0 drift。型別／lint／匯入／格式等完整 gate 保留。這是該程式候選的獨立審查，不能外推為整份規格結案。
- 真實 [Inseason source run 36958474336](https://github.com/leo110047/fantasy-basketball-assistant/actions/runs/36958474336) 的 Linux、macOS、Windows 全部成功；[Check run 36958474481](https://github.com/leo110047/fantasy-basketball-assistant/actions/runs/36958474481) 成功。Windows 完整測試 1,197 passed／730.68 秒。三份 artifact 已下載，型別、欄位、字串及排序逐項一致，數值有限且最大差 0（要求絕對差≤1e-9、相對容差0）。比較收據與 SHA 在 Git 外 `portability-fba7fdc-comparison.json`。
- Windows 原生實測修正包含系統 DLL 靜態依賴、venv launcher 與 interpreter PID 分辨、強制 launcher 終止後 child 清理、編輯器未共享 DELETE 時的 typed failure／原檔與歷史保留／關閉後可重試，以及允許分享時的原子替換。reader 使用既有 log writer lock 取得完整紀錄，沒有改產品日誌語義。
- Windows 無 listener 的控制案例在 1 秒預算得到 TimeoutError，5 秒預算約 2.014 秒得到 ConnectionRefusedError／WSA10061；控制 socket 僅 bind 後 close，不是曾 listen 的服務。runtime 測試改用一個 5 秒總停止 deadline 涵蓋 quit/kill、communicate 及真正 connection refusal，沒有放寬產品計算期限。真實 HTTP 200、401、listen 但不回應的三個反例均須拒絕，不能把 Timeout／HTTPError 當停止。
- 暫時 probe 的原生證據在 Git 外 `windows-d7098bf-launcher-readback.json`、`windows-bb95908-refusal-readback.json` 及完整 job logs。歷史審查 `d36223cf`／`99efc7f5` 的舊 closure artifacts 未改寫；新的完整合併審查涵蓋修正與實測，不宣稱舊 typed closure 已自動更新。
- 本機合成 Chrome 已驗證同日連續換人、IL/F3 事件時間軸、保存計畫冷啟動沿用、DST、手調／忽略到期與狀態變動、保存／撤銷。provider transport 拒絕呼叫，credential 僅在 memory fixture vault；不是 OS 憑證庫或 live Yahoo 證據，驗收服務已正常退出。
- E/F1：129 條登錄，142 個 Python 檔／464 候選以及 1 個 C++／10 個 JS 來源人工盤點 pending=0；20 個實際 UI scalar trace 獨立手算≤1e-12，另補四組 array／類別／手調／Log loss trace。此為公式所有權與白箱驗收，與原規格功能 F1 的歷史預測校準分開。

## 3. 正式來源、歷史與品質缺口

使用者已送 Yahoo API 申請；目前尚無核准、憑證核發、OAuth 或真實唯讀同步的證據。不重新要求申請，也不讀 OS 憑證庫或要求在對話貼 token。

使用者已表示會申請 ScoreTape 免費方案並自行執行驗證。本機 `probe-scoretape.py` 只在固定官方來源做有界 GET、隱藏輸入 key、不保存 key；目前未收到 wire receipt。步驟見 Git 外 `scoretape-access-plan.md`。尚未看到實際欄位前不實作假 wire adapter；最近七天存取也不足以完成跨季回測，排名／回歸日供給尚未確認。

原 2025–26 研究資產已找回，五份 players／schedule 資料 SHA 與 manifest 一致；318 位球員、1,446 案例重播≤1e-12 零差。另做跨季「公式」驗證：325 人／1,473 訓練案例、317 人／1,446 驗證案例，11 欄位×5 checkpoint 共55項通過基準。季後擷取資料缺 DNP／傷情／排名／prior 當時公開時間，也不是目前引擎的因果策略回放；不可倒填 known_at。目前124參數葉值仍為 initial-uncalibrated／Assumption，沒有安裝新係數。官方 NBA 資料的可用性／用途限制另記 Git 外 `official-nba-history-readback.md`；不當成已取得合法兩季完整歷史。

B2 舊品質反例已重現並修補：固定 30 seeds、100 samples、一先發／三候選／四天、256 合法整週組合，舊 daily_exact_coordinate 有 13/30 低於全枚舉，最大 4pp；seed12012 由 `[p0,p1,p0,p0]` 的 0.51 改為 `[p0,p0,p2,p0]` 的 0.55。新 `weekly_lineups` 完整搜尋所有合法組合，以保守上界及平手規則剪枝；不再因空間超過 64 退回逐日近似。64 改作每節點直接列舉門檻，抽樣／候選／產品期限不變，逾時不發布部分結果。新增 `tests/test_inseason_weekly_quality.py` 涵蓋兩計分模式、四天 256 與六天 4,096 完整組合、TO 反向、投罰三位小數、鎖定／未來換人、同分／零校準、浮點上下界與中斷。這是固定對手及模型下的搜尋正確性；不等於 recall≥95%、歷史 holdout、真實勝率或效能驗收。兩季跨功能參考值的變更另以獨立計分／完整枚舉核對全部 124 個實際整週呼叫、96,843 組合法排陣，最大空間 2,401 組，原始分數與最優值差≤1e-12；核對後才更新 portable reference。新批完整檢查／獨立審查與提交狀態以 Git 外 current-status.md 為準。M5 多 IL 已改為完整合法釋出路徑比較，以同一 ROS 起點估值；原 30 例9例落後、最大0.08的反例修正後均與9條完整路徑的獨立名單 oracle 相符。另核對同日／分日及兩計分模式；另補多名回歸與明確未來換人並存的完整路徑 oracle。本輪再補三名 IL 回歸：同日／分日 × 兩模式 × 三 seeds 的 12 例，每例均與 27 條完整合法釋出路徑一致。初審 5ffb1269 的唯一失敗是未變的競標 solver deadline；原條件單獨競標重播通過，但相同 digest 正式 closure 被工具拒絕，不能當成已結案。完整候選審查／提交狀態以最新入口為準，不沿用前版 CI。此最優限於既有回歸日期政策及目前每週估值模型，B2 現改用完整條件式整週搜尋，F3 篩選／beam 的真實 recall 及來源／歷史品質仍未完成。不能以外部資料尚缺概括所有未完成項。

本輪因果／F3 補修：`checkpoint_history` 以每場首次公開時間固定第 N 場決策點，分開解析當時版本與最終結果；晚到更正不再回寫訓練輸入或移動旗標決策時間。晚公開先驗拒絕，季後擷取不能產生早期檢查點。原缺陷回歸先取得 7 failed，再補出版順序／未來目標／季後擷取控制。F3 多步 z 篩選改以同一原始名單累計序列；replay 與 live 共用必勝週／類別／ROS 政策，首次篩選的基準包含全部合法 drop，沒有可獲益方案的案例不算成功，完整候選池受既有 oracle 上限約束。

前版 `a3c4a26` 的正式預設 1,000／200 samples、shortlist 10、drop 3、beam 3 合成枚舉未達標：一次換人 19/24（79.17%），兩次換人 7/12（58.33%）；另留的 12 個單步 seeds 為 9/12。原 24 例診斷集合修正前後皆為 17/24，不宣稱該版整體 recall 提升。原始結果保留在 Git 外 `causal-f3-results.json`，不能以缺外部資料解釋這些已重現的搜尋漏解。

本輪使用者確認「先確保搜尋品質，再處理速度」。F3 改為低貢獻 drop／z／分數優先的完整合法搜尋，原 shortlist／beam 不再刪除候選與前綴；合法但暫無增益或暫不符長期保護的前綴也全部延伸。按換人次數保留有界的最佳發布清單，沒有縮樣本、縮候選或放寬期限。固定四個反例先取得 4 failed，再全部找回既有全枚舉最佳分數；完整引擎品質測試不套整個 F3 的產品期限，與原期限下能否完成分開記錄。完整性測試另核對所有訪問方案 ID 與獨立列舉一致、無重複、排序及各深度發布上限；已產生正增益後才逾時／取消也不得保存部分結果。40 個預先固定案例（原 36 加四個新雙步案例）的完整引擎品質與原產品期限實測分列於 Git 外 `f3-complete-matrix.json`，產品逾時不算產品召回成功。真實因果歷史與策略效果仍缺資料。

新 replay 報告標記 `search_method=complete`，單步 recall 量測完整搜尋後的發布清單，多步沿用相容欄位 `beam_recall`。舊報告缺少方法欄位時保持 `z_beam`。仍以完整合法參考作分母、排除無獲益控制，原 95% 與最少案例門檻不變；故意漏回最佳解的測試仍必須得到 recall 0。兩季 portability 新參考僅改 F3 清單及搜尋參數的政策出處雜湊，共同方案分數與其他功能相同；獨立核對全部 388 次整週呼叫、115,546 組排陣後更新，原始證據在 `complete-f3-portable-oracle.json`。本輪完整檢查、獨立審查與交付狀態以最新 `current-status.md` 為準，不沿用前版綠燈。

兩季 portability 參考只更新 F3 候選及排序，其他功能與共同方案分數相同，兩季最佳分數不變；獨立核對 124 次整週呼叫、98,076 組合法排陣後才更新。原始失敗、差異與完整 oracle 保留在 `causal-f3-full-verify.log`／`causal-f3-portable-diff.json`／`causal-f3-portable-oracle.json`。

本機兩季原檔共 147,201 筆逐場資料，0 筆含 `known_at`；`historical-causal-readiness.json` 列出兩季來源 SHA、欄位、擷取時間與缺少的先驗／傷情／Yahoo 名單及比分。上述修補不是已完成真實因果 holdout、策略優勢或參數校準。完整檢查／獨立審查、提交與三平台 CI 以最新 `current-status.md` 為準。

## 4. 完整追蹤清單

「本機已修」表示程式／合成驗證；「未驗」保留真實來源、平台或品質證據。Python 位置相對 `src/fba/`，測試相對專案根目錄。V5 的三平台合成證據已補，其餘完成條件保留。

### A. 資料入口、同步與儲存

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| A1 | 未完成：完整免費且條款允許的 NBA adapter、正式資料初始化。 | `data/inseason_sources.py`、`inseason/session.py`、來源設定 UI | 選定來源，實作真實 wire adapter、認證、分頁、限流、球員／逐場數據／賽程／傷病及回歸日；以授權實際回應從 clone 完成 F1/F2，再串 Yahoo。明列來源沒有提供的欄位。 |
| A2 | 本機轉接已補；真實 Yahoo 回應、設定與首次同步未驗。 | `data/yahoo*.py`、`tests/fixtures/yahoo-example.json` | 使用去識別且不含 token 的實際錄製回應，涵蓋設定、名單、對戰、自由球員、交易與比分；完成真實授權、設定確認及首次同步。 |
| A3 | 本機已補持有率／變化／來源時間／未知；live wire 未驗。 | `data/yahoo.py`、`data/yahoo_normalize.py`、`inseason/today.py` | 抓取並正規化持有率與資料時間；缺值不可假裝為 0，畫面能辨認未知。 |
| A4 | 本機已補增量／歷史比分輪轉／交易 cutoff；真實同步與更正未驗，效能暫停。 | `data/yahoo.py`、`inseason/session.py` | 增量同步、分頁與舊比分更正策略；測試季中／季末及大量交易，14 隊完整同步 ≤60 秒、≤100 次請求，必要失敗清楚顯示。 |
| A5 | 有界 payload 查詢、原始歷史保留已補；索引持續成長、保存策略與長季驗收未結案。 | `data/storage.py`、`inseason/operations.py::bootstrap` | 有界查詢／分頁與明確保存策略，量測長季資料量。不得直接刪除原始快照、帳本或歷史預測來通過效能。 |
| A6 | 本機已補可靠 identity／唯一 fallback／歧義確認；真實名單 100% 對照未驗。 | `contracts/yahoo.py`、`data/yahoo_normalize.py`、`inseason/session.py::map_player`、UI | 以可靠識別欄位做唯一匹配，歧義要求確認；顯示姓名／球隊等識別資訊。名單球員 100% 對照、自由球員未對照可見，換季不用逐人重輸。使用者估計約 190 名單＋400 自由球員。 |
| A7 | 缺排名的未知行為已修；合法且可替換的正式 rank adapter 未完成。 | `inseason/trades.py::evaluate_trade`、`contracts/inseason.py::SeasonPlayer` | 選定合法來源並實作可替換 adapter；明訂缺排名的可用性／呈現，避免一名球員令全部搜尋無結果，也不得捏造排名或接受機率。 |

### B. 效能與排陣／搜尋品質

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| B1 | 每日精確枚舉／批次／重用有獨立 oracle；效能驗收暫停。 | `core/lineups.py`、`inseason/matchup.py`、`formulas/categories.py` | 先 profile；消除重複工作、適當批次化與重用合法組合。保留 ≤15 人與窮舉一致的正確性證據，不能只縮小搜尋或放寬 timeout。 |
| B2 | 舊30例13例落後已修；改為完整整週搜尋及保守剪枝，四天／六天獨立窮舉與中斷回歸通過。真實資料品質／效能仍未驗。 | `inseason/matchup.py::optimize_total` | F3/F4/F5 共用完整條件式整週結果；上界／同分規則與獨立全枚舉一致。成功只代表固定對手、名單政策及抽樣下的最優；真實品質與效能需另驗。 |
| B3 | ROS 抽樣、共同亂數、跨請求失效與冷算一致已驗；長季效能暫停。 | `inseason/season.py`、`Simulation`、`session.simulation` | 使用明確的 ROS 抽樣契約與共同亂數；快取按同步、聯盟、參數、帳本、名單／生效日等正確失效。抽樣數不同不能直接共用不相容的 draws。 |
| B4 | 保守上界與完整枚舉 top-10 一致有證據；真實規模效能暫停。 | `inseason/trades.py::search_trade_bundles` | 可驗證的上界／候選篩選；小聯盟多換多前十名與完整窮舉一致，記錄真正省略的昂貴計算數，不只統計最後呼叫 `evaluate_trade` 的次數。 |
| B5 | 新今日契約與沿用已修；F3／交易／Today 真實 p50/p95 未驗，效能暫停。 | `inseason/recommendations.py`、`inseason/trades.py`、`inseason/today.py` | 固定 14×13／10 starters／150 FA 與真實聯盟 p50/p95，F3／交易／Today 分別達 15／30／1 秒。Today 依後續指示只沿用有效 F3／IL 決策，涵蓋 API 讀取／計算／序列化／保存，不重新逐名 ROS。指定一隊 2 換 2 仍需 ≤30 秒；效能驗收目前暫停。 |
| B6 | 500 人合成入口有量測，非真實聯盟驗收；其餘效能暫停。 | F1/F2、同步、所有計算入口 | 500 人有效預測 ≤1 秒、球隊頁 ≤300ms、手調至更新勝率 ≤2 秒；含冷／熱資料與硬體資訊。產品 budgets 保持明確，不用延長 Goldband 時間宣稱改善。 |

### C. 預測、模擬與校準正確性

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| M1 | pooled exposure／誤差／旗標同源本機已修；門檻與命中率無 holdout。 | `inseason/projection.py::player_flags`、`formulas` | 定義正確的總量／曝光量估計與抽樣誤差，納入不同上場分鐘、零計數與雜訊案例；建議乘數同源，公式登錄並有獨立數值答案。門檻須實證驗證。 |
| M2 | 雙位置去重、零／部分出賽分鐘分配已修；prior groups 仍粗且為 Assumption。 | `inseason/projection.py::fallback_prior`、`inseason/adjustments.py::redistribute`、參數檔 | 位置／分鐘 peer 分組與資料依據、無 peer 明確失敗、雙位置去重；補部分傷病與隊伍分鐘分配的情境驗收。不要把粗群組描述成已校準先驗。 |
| M3 | 缺完賽比分涵蓋時明確不可算；真實 Yahoo 不漏算／不重複仍未完成。 | `inseason/matchup.py::actual/total/daily_draws` | 核對 `through` 與來源比分的真正語義；覆蓋已完成、進行中、當天多場、官方更正。不漏算、不重複加入部分已含的比分，不猜歷史 Yahoo 先發。 |
| M4 | 無 history 的隨機 frozen prior 本機已補；相關性及樣本外品質未驗。 | `inseason/matchup.py::game_draw`、來源歷史資料契約 | 開季、新秀／新升上來球員可計算；補足歷史或明確定義先驗預測抽樣與不確定性。不能用全零／確定常數偷偷代替未知，也不能把未來資料帶入回測。 |
| M5 | 本機已修 IL／未來換人合併事件、完整合法後續、容量／保護／鎖定延期、再取得與冷啟動 Today 沿用；本批已補同日連續換人的合成 Chrome 流程。多 IL 完整合法釋出路徑比較已補獨立合成 oracle，另補三 IL／27 路徑的 12 例，使用共同 ROS 起點；日期政策及每週估值模型的品質邊界仍保留，真實 Yahoo 未驗。 | `inseason/matchup.py::roster`、`inseason/season.py`、`inseason/today.py` | 包含預計回歸、移出 IL、容量、保護及必要丟人的合法轉換；與未來換人共用時間軸，動作與條件排陣一致，不把球星 ROS 當零。合成 oracle 不能替代真實來源與完整搜尋品質驗收。 |
| M6 | 類別／整週參數、匯出及擬合分開本機已修；holdout 未完成。 | 參數契約／defaults、`inseason/matchup.py`、`inseason/review.py::calibration_history/refit_history`、backtest | 分開類別 0.82／整週 0.80 的規格初值及出處；匯出兩層級的預測與結果，分開擬合、展示與樣本外驗證。舊版積分不可當新整週勝率訓練資料。 |
| M7 | 自建接受模型案例、分離／不識別檢查及求解已驗；真實提案機率未校準。 | `formulas/fitting.py`、`inseason/operations.py::refit_acceptance`、保存的 acceptance-fits | 自行建立正常、分離與不識別案例，驗證係數／需求方向、殘差、log loss、顯示與既有報告處理；真實提案樣本另作驗收。不要把合成訓練擬合說成真實接受機率校準。 |

### D. 回顧與回測

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| R1 | 固定 origin／去重／原始歷史／類別 cohort 已修；本輪補完整規則雜湊，檢查所有原始決策並拒絕已知不符。無雜湊舊紀錄與未保存驗證狀態的舊報告可相容讀取；單週／累積畫面已明示歷史規則未確認，匯出與擬合保留相同限制。本機 Chrome 已核對未知／已驗證控制案例及正常退出；原始未知規則仍無法補證。 | `inseason/operations.py::week_result`、`inseason/review.py::weekly_review/cumulative_review` | 定義固定評估 cohort／去重鍵；同一資訊重開頁不改變權重，保留不同決策與原始歷史。完整規則變更不能去重為同一預測或套用現在規則重評；缺歷史規則出處應明示限制。 |
| R2 | 同步成功後保存當時預測、內容去重／失敗隔離本機已修；不補造過去預測。 | `inseason/session.py` 同步完成路徑、`inseason/operations.py::complete_reviews` | 不依賴頁面瀏覽的當時預測保存；同步、排程、重啟不重複寫入。不准用事後資料補造事前預測。 |
| R3 | 分群樣本量、不等權重 n_eff 與提醒有獨立公式答案；真實區間 coverage 未驗。 | `inseason/review.py::calibration_bins/weekly_review/cumulative_review` | 明確樣本量／不確定性政策與參數出處，覆蓋少量與足量案例；不是直接提高門檻或隱藏提醒。 |
| R4 | 已修檢查點洩漏及 recall 高估；F3 改完整合法搜尋，四個漏解反例已修，品質與逾時分開量測。真實凍結時間序列、跨季 holdout、策略比較仍缺。 | `inseason/backtest.py`、`contracts/inseason_backtest.py`、`inseason/replay.py` | 用真實凍結時間序列跑實際引擎，前季訓練／後季 holdout；5/10/20/30/40 場預測誤差、旗標命中率、出賽 Brier、10 分箱≤5pp、規格 Brier 基準。換人 recall≥95%、與不動／公開排名策略比較也須真實因果重播，逾時不當成成功。 |

### E. 畫面、公式與參數

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| U1 | 本機已補對手／時間／場次／基準／raw 機率；真實聯盟整合未驗。 | `apps/inseason/static/views.js`、結果契約 | 明確基準線與所選週資料，不和「無手調」基準混淆；數字可追溯且經瀏覽器核對。 |
| U2 | 本機合成 Chrome 已驗忽略／忽略到期、傷情改變解除舊忽略、手調到期恢復、新球隊頁／舊手調提醒及接受／撤銷；供應商設定差異恢復仍待真實 Yahoo。 | UI、projection flags、同步設定差異 | 依規格整理待確認事項、來源／理由／操作，核對忽略／到期／狀態改變後行為。 |
| U3 | 本機完成 20 數字獨立手算、實際手調／撤銷、UI 與模擬值同源；合成資料邊界。 | `apps/inseason/static/views.js::playerCard`、`inseason/projection.py::blend_player/effective_projection` | 清楚分開先驗、混合、手調及最後由命中率重算的 made；抽 20 個 UI 數字獨立手算（至少 3 位小數）並驗證傳給模擬的值。 |
| U4 | 合成資料的實際 Chrome 已驗跨日、IL／F3 合併、DST 春秋、S-001、新舊回顧與提案更新；本批另完成同日連續換人的有效合成授權／保存計畫沿用流程；真實 Yahoo 未驗。 | today／trade／review UI | 聯盟美東日期、使用者台北顯示、DST、跨午夜、交易內容及舊版 Brier 標籤都實際操作讀回。 |
| F1 | 129 條登錄；143 檔／472 Python 候選及 C++／JS 人工盤點 pending 均為 0。兩應用隨機 20 個實際 scalar trace 已手算／展開核對；逐功能核對另補四組季賽 trace，相關候選完整檢查及獨立審查已通過；公式所有權／UI trace 本機範圍結案；不包含原規格功能 F1 的歷史校準。 | `formulas/*`、`design/formula-ownership.json`、相關 tests | 全範圍語意所有權與登錄一對一；業務公式只在 formulas，編排／搜尋／驗證有具體理由；UI trace 同源。檢查不能自動證明語意正確。 |
| F2 | 124 參數葉值逐鍵核對，初值無不符、出處已修；實證訓練／驗證對應仍未完成。 | defaults/parameters.json、回測報告 | 逐鍵核對初值、來源、訓練／驗證季與限制；已找到原規格指定的 `/Users/leo/fantasy-research-2026-27/claude-report-v2/evidence-2026-09-27/` 程式／JSON，重播 318 位球員、1,446 案例與原結果在 1e-12 內零差異。原研究是同季擬合，ESPN dump 於 2026-09-25 季後擷取，不能證明歷史公開時點；也不能當目前 strict-win 引擎的跨季校準。無證據不能改標已校準；OREB 等未回測項另列。 |
| F3 | 10 隊、不同位置與 OREB／A/T／H2H Each Category 的合成 F0–F6 本機已有驗證。 | `tests/inseason_support.py`、`test_inseason_portability.py`、跨功能測試 | 真正改類別公式（含比率／A/T）、位置、10 隊、H2H Each Category，F0–F6 跑完且合法；只改設定，不在程式塞聯盟特例。 |

### F. 交付與實際環境

| ID | 未完成的驗收 | 完成條件 |
| --- | --- | --- |
| V1 | 未完成：真實 OAuth／refresh／撤銷／限流／時效／設定差異恢復。 | 使用授權測試帳號，安全保存錄製 fixture；驗證只讀、來源時間、失敗訊息及恢復。合成資料或 mock token 不算。 |
| V2 | 部分完成：windows-latest 已實測原生 DLL、實際 interpreter PID、單一實例、正常重啟、同一 5 秒 deadline 內強制清理／HTTP connection refusal、編輯器 DELETE sharing 與失敗復原。Windows 11 保留 port、GUI 共存及寫入中斷整體驗收仍缺。 | 真實 Windows 11 驗證 socket 互斥／保留 port、10013 換 port、單一實例、同時雙開、正常重啟、強制終止後 5 秒內無殘留、寫入中斷復原；共用 runtime 也要驗證競標桌與季賽助手共存。 |
| V3 | 未完成：無 Python 的乾淨 macOS／Windows clone 與自己的正式資料／憑證。 | macOS／Windows 無 Python 環境依 README 安裝必要的 uv、由 uv 取得鎖定 Python 後雙擊啟動；中文／空白路徑、自己的憑證、首次資料取得／同步。先前 macOS 副本測試機已有 Python 3.13.7，不算無 Python 證據。 |
| V4 | 合成 Chrome 正常結束、CI 命令檔 --help、程式化退出／強制終止已驗；實際雙擊第二次、關分頁／命令視窗及卡住後 GUI 診斷仍未完整驗收。 | clone 命令檔實際雙擊第二次時可見診斷，不偷開另一實例。原「打包版沒有結束選單」已被 clone 交付取代；仍要驗證現有「結束助手」、關分頁及命令視窗的行為。 |
| V5 | 已完成合成 fixture 範圍：fba7fdc 的 Linux／macOS／Windows 完整 CI 成功，三份 artifact 的欄位、排序一致；每對 1,121 個數值、三對共 3,363 次比較，最大絕對差 0。此證據不涵蓋真實資料或乾淨 Windows 11 clone。 | 真正執行 macOS／Windows／Linux 完整工作流程；同快照、帳本、參數與 seed，排序相同、數值差≤1e-9。CI YAML 存在、單機 golden fixture 更新都不算跨平台通過。 |
| V6 | 未完成：原規格逐條最終整合驗收與里程碑，不因本機審查通過而結案。 | 逐條重查原規格，尤其跨功能手調／撤銷、因果性、錯誤可見、秘密掃描、公式手算、設定可替換、來源時間與日誌；附真實環境證據、未測項與實際 p50/p95，再開獨立審查。 |

## 5. 接續順序與操作

1. 本輪搜尋批次已提交 `6797390`，Goldband `e1723844-66e4-41f5-8df7-5d8ccbe96c4e` 完整 1,443 tests／513.16 秒、兩 required verified-pass、0 findings；309檔0 drift。R1 後續批次與推送／CI 狀態以最新報告入口為準。核對 git／遠端 main、最新證據入口與本文件。完成已審查候選的分批 commit 與推送，讀回遠端 SHA；審查期間固定候選。未變更範圍沿用相同候選證據，新增範圍做相符驗證及 Goldband 獨立審查。
2. 取得 ScoreTape wire receipt 後核對欄位與合法存取範圍，補 A1/A7 的真實 adapter。Yahoo 核准後以使用者提供的本機設定完成唯讀 OAuth／refresh／同步及去識別 fixture，補 A2/A6/M3/V1。必要秘密只由使用者在設定流程輸入。
3. 取得當時公開且可用的歷史後，跑實際引擎的因果 holdout／策略比較及校準；保留資料限制。B2 程式已改完整條件式搜尋；繼續驗證 F3 recall 與真實策略品質，不以合成 oracle 代替歷史驗收。
4. V2–V4 仍需 Windows 11 保留 port／GUI 共存、無 Python 的乾淨兩平台 clone 與實際雙擊全情境；CI runner 及 --help 不替代此證據。V6 依原規格逐條整合驗收，效能項目依暫停指示保留，未達標不結案。

```sh
cd /Users/leo/fantasy-basketball-assistant
git status --short
git log -5 --oneline
.venv/bin/python scripts/verify.py
```

初始化開發環境依 README 使用 `uv sync --locked`；完整驗證需 Node.js 和 C++ 編譯器。使用者 clone 啟動見 [季賽助手說明](inseason.md)。Goldband 依安裝 skill 使用 `$goldband review code` 的完整 execution evidence；審查輸出核對 typed evidence 與 completion authority，不以 exit 0 代替，不修改簽章／lineage 或切 semantic-only 迴避缺口。遇到必要憑證或實際平台缺口，保留未驗項與所缺輸入，繼續不依賴它的工作；不要把 mock、單機或 CI 證據外推至 live／GUI／歷史。
