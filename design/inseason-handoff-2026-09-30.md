# 季賽助手交接：未完成工作與驗收缺口

更新日期：2026-10-02。專案：`/Users/leo/fantasy-basketball-assistant`。

目前最新程式候選已分批提交至 `49e9bc9`，通過 1,193 項本機測試與獨立審查 `8341a8ab-9167-4f54-86a8-5939de335d26`（兩項 required verified-pass、0 findings）；305 檔提交前 0 drift。使用者於本輪授權「直接推到 main」，已推送並讀回遠端完整 SHA `49e9bc9825235ba66ea66ac5b8eb308a920d268b`。三平台 CI 已啟動，狀態見 Git 外入口。以下較早「未推送／未提交／仍待審查」是該批當時狀態，不代表目前 HEAD。整份規格仍未完成。

目前契約與假設見 [計算與資料契約補充](inseason-remediation-2026-09-30.md)。較早 IL／換人時間軸與聯盟規則驗證已通過 Goldband 複審，分別提交為 `2334ec4`、`88c96cb`；當時僅在本機，本輪已隨 main 推送。下列清單更新至目前本機證據，完整驗收仍未完成。原始報告保留於 Git 外，最新入口為 `/private/tmp/fba-continuation-report/current-status.md`。使用者只接受免費且條款允許的來源，沒有提供原始案例，要求自行建立驗證。效能工作已明確暫放，未結案。

使用者後續已調整今日頁契約：沿用已計算的換人／IL 計畫，不再逐名球員重跑整季移除搜尋。一般週不得降低剩餘整季強度；只有輸球即淘汰或確定失去晉級機會、且贏仍有路徑時才優先本週。F3 先篩選對目前隊伍貢獻較低的球員。以下 B5 的原始「今日頁須納入所有丟人估值」要求依這項後續指示替換，其他未驗收條款仍保留。

自動交易搜尋也已依使用者指示改為先比較公開排名換算的整包價值，排除明顯不對等的組合，再計算隊伍適配。初值為較低估值至少達較高估值的 70%，使用者可在 50–100% 間調整、保存；缺排名不捏造估值，手動指定交易仍可看量化影響。此項後續指示替換原 F4 無條件評估所有組合的範圍；多換多前十名的品質驗證與效能缺口仍需個別證據，不能只憑減少組合數宣布完成。

使用者後續明確選擇保留通過價值門檻的完整交易搜尋，繼續改善精確演算法，並指出競標桌既有依工作量啟用核心的機制。候選已沿用該 worker 政策、共同基準與摘要／詳情流程；計時與最新審查仍以 Git 外報告為準，不沿用舊候選的綠燈。

## 2026-10-01 接續工作（本機候選）

前批處理 F1 與同日換人 UI 缺口，已分批提交為 `157db0a`（測試同步）、`a2d5e7b`（公式抽取）、`f840ad5`（所有權盤點）、`c8e97a4`（文件）；全部未推送。原生完整檢查初次通過 1,142 項測試（264.70 秒）；Goldband 初審發現盤點漏記外層負號，以及隔離測試讀到背景執行緒正在寫入的半筆日誌。兩項已重現並修正，修補候選在本機與隔離環境各通過 1,145 項測試，正式 closure 已關閉兩項 finding。之後另做 81 項語意分類與 NumPy 呼叫漏記修正；此提交候選完整檢查 1,150 passed／301.47 秒，獨立審查 `68a45bda-4a12-4d38-a3b1-cccb50519b64` 的兩項 required evidence 均通過，沒有新 findings；四批合併內容通過驗證，未宣稱中間 commit 各自跑完全部檢查。詳細狀態見 Git 外入口 `/private/tmp/fba-continuation-report/current-status.md`，不沿用較早候選的綠燈。

- 24 條公式抽到共同 owner 並補登錄／手算答案，登錄總數由 90 增至 114。樣本摘要、類別／雙十、保守區間、戰績／晉級、健康模型、競標抽樣、成交價格與球隊預算已有呼叫處搬移。
- 建立 [公式所有權盤點](formula-ownership.md) 與全 Python 數值候選變動檢查，逐條分類 81 項原 pending；補檢 NumPy 最小／最大／加總等呼叫後，140 檔／449 候選中仍有 107 `pending`。這不是 107 條確認漏登錄；F1 仍未結案，native 機會成本、JavaScript 與最終 UI trace 仍需逐條核對。
- 同日連續換人的實際 Chrome 合成流程已讀回：先加入 p7／釋出 p2，再加入 p8／釋出 p7，最終名單 p1／p6／p8；IL 必要釋出與啟用在前。保留原樣本數和產品驗證；有效合成 credential 只留在 fixture vault，所有 provider transport 明確拒絕呼叫。沒有真實 Yahoo 驗收。Today 冷啟動後若重跑 IL／ROS 會直接失敗，本次成功沿用已保存 F3。
- 審查修正：數值候選簽章保留完整函式內容，並加入負號／外層 abs 與單獨負號的失敗後回歸；日誌測試使用既有 writer lock 取得完整紀錄，保留所有原始保存狀態斷言。沒有修改產品的交易保存／記錄行為。
- 本輪原始證據入口：`/private/tmp/fba-continuation-report/`。Chrome DOM／截圖為 `chain-today-dom.txt`、`chain-today.jpg`。驗收伺服器已經由實際「結束助手」正常關閉。

## 2026-10-02 持續工作

8 條健康／native／誤差公式抽取已通過完整 1,169 項測試（macOS 273.99 秒、隔離 286.18 秒）與獨立審查 `3b0a7af3-a147-4076-964e-96e9de4325d0`，兩項 required evidence fresh verified-pass、0 findings，302 個候選檔案提交前 0 drift。分批提交為 `a2aff0b`（共同公式與 native）、`a1ddd86`（誤差／歷史 prior trace）、`4b676ee`（所有權與來源文件），全部未推送。

接續未提交候選另抽取七條，登錄總數 129；142 個 Python 檔／464 候選已逐條分類，Python pending 為 0。新增 NumPy 加總順序、布林健康矩陣、made ≤ attempted 等獨立答案及回歸。另人工核對 1 個 C++／10 個 JavaScript 來源並加入檔案變動檢查；拍賣頁 `presentation.js`、`view.js` 仍有兩個 pending。Today 類別影響保留 raw 差與校準乘數兩段 trace，合成 Chrome 展開實際代入值相符；保存 F3／IL 的唯讀合成流程正常結束。這批已通過完整 1,176 項測試／303.82 秒與獨立審查 `b967c110-58f5-43ce-8c59-da5a33aadb6e`（0 findings、兩項 required verified-pass），303 檔提交前 0 drift。分批提交 `f2dfbc8`、`818633e`、`86a32fc`，全部未推送；F1 仍未結案。

後續拍賣未提交候選已消除兩個 JS pending，前端價差／折扣／出價空間與房間／分支餘額均由後端共同公式和 trace 提供；人工盤點 pending 為 0。重點價差門檻沿用舊值並放入市場設定，相關 schema 同步。實際 Chrome 完成跨兩應用隨機 20 個 scalar trace 獨立手算／逐一展開，誤差 ≤1e-12；買／不買及房間摘要也讀回。拍賣候選完整通過 1,185 項測試／292.59 秒，獨立審查 `2ad24675-08fc-48f9-92ec-73935d864e2a` 為 0 findings、兩項 required verified-pass。304 檔提交前 0 drift；分批提交 `ad00944`、`3980741`，未推送。詳細範圍與限制見 `formula-ownership.md`。

接續季賽候選補每週我方／對手總量與比率、每週／累積分箱平均、手調乘數、接受模型 Log loss 的共同公式 trace；舊紀錄維持可讀，不補造證據。每週 trace 僅保存兩隊平均數據，不保存 sample × day × player draws。相關 81 項測試、38 項所有權／品質／獨立答案檢查通過，型別 0 errors。合成 Chrome 已讀回四組實際代入值與結果，服務皆正常結束；初次完整檢查為 1,192 passed／1 failed（292.11 秒）：舊 trace 重播測試只接受 scalar，未支援新 array trace；已依登錄 ID 分派至 scalar／array owner，保留原 scalar 容差及所有輸出斷言，array 使用 1e-12 絕對容差。修正後完整通過 1,193 項測試／295.29 秒；獨立審查 `8341a8ab-9167-4f54-86a8-5939de335d26`（candidate `85ee5773798d505c8403ea919b526097d7fc6a935615ba793643878a30c35467`）為 0 findings、兩項 required verified-pass、completion-authorized=true。305 檔提交前 0 drift，分批提交 `a41e030`、`043fb6f`、`49e9bc9`。

找到原規格指定研究程式／JSON 後，離線重播原研究 318 位球員、1,446 案例，結果在 1e-12 內零差異。另以目前登錄的單欄位混合式做前季選 k、後季固定 k 的診斷：325 位球員／1,473 訓練案例，317 位／1,446 驗證案例，11 欄位的合併驗證 MAE 均較 prior 及現行 k 小。追加核對 11 欄位 × 5 檢查點共 55 項，固定 k 的診斷誤差均不高於 prior／當季基準。它不是實際引擎的因果 R4，不含零分鐘、傷情／出賽、排名與策略；沒有套用新 k。124 個參數葉值保持原樣，只補原研究與未校準限制的具體出處。

來源調查可繼續，但目前尚未取得免費、條款適用且涵蓋完整即時傷情／排名／歷史的正式資料入口。SportsDataIO Discovery Lab 有免費上一季個人研究方案；API-Sports 有免費額度，但用途授權和歷史覆蓋須另核對，都需要使用者帳號／key。不可把 demo key 或過時傷情當 live 證據。追加核對 ScoreTape 官方 docs／terms，允許本人研究／分析／建模，免費七天窗口且公開 health 已取得 status ok；列為當季前向來源優先待驗，使用者已選擇自行申請 Explorer 並執行本機唯讀驗證；仍待實際 wire／NBA coverage，不能補回跨季歷史。Yahoo OS 憑證讀取曾遭工具自動審批拒絕，沒有繞過；使用者已於 2026-10-02 回覆 Yahoo API 申請已送出，目前等待審核／憑證核發，尚無真實 OAuth／同步證據。申請不再列為待辦。

本輪另以目前正式程式、原參數與 memory vault 在實際 Chrome 核對 U2：忽略到當日後提醒消失；過期忽略重新顯示；傷情 healthy→INJ 後舊忽略不隱藏新旗標；到期 60 分鐘手調恢復模型 30 並標「已到期」；球員轉至 NBA9 後出現在新隊，舊手調標「球隊已變更，請確認」。從旗標建立 30 分鐘手調，預覽 60→30、保存後生效，撤銷後恢復 60 與原提醒，保留新增／撤銷帳本。此未同步 fixture 只驗 F1/F2，F3–F6 正確停用。五個服務皆由實際「結束助手」退出 0；原始 `flag-ui-*.txt`／`.jpg` 在 Git 外。沒有讀 OS 憑證庫或呼叫 provider。

## 1. 接手時先知道的事

**整份規格尚未完成，現況不能當成一般使用者 clone 後即可使用的成品。**
公式所有權／UI trace 的本機驗收已完成；主要缺口仍是真實 NBA／公開排名入口、真實 Yahoo、因果歷史回測／校準及實際交付平台。大型排陣與換人搜尋的品質驗收也未完成。
接續前一批的本機與隔離容器各通過 1,127 項測試，Goldband 複審結案；本批最新證據見上方接續段落與 Git 外入口。這些證據不代表真實供應商、原生 Windows、歷史 holdout 或整份規格通過。效能驗收暫停。

權威需求：

- 原規格：`/Users/leo/Downloads/季賽助手：功能規格與驗收標準.md`。
- 共用層原規格：`/Users/leo/Downloads/Fantasy 籃球助手重構規格與驗收條件.md`。
- 本文件涵蓋使用者 2026-09-30 的完整缺陷清單及目前已知的交付缺口。未逐項重做整份規格的驗收，所以未列出新缺陷的條款也不能自動視為通過。
- **交付方式已改為 clone + 雙擊命令檔**，取代安裝檔／打包流程。不要重新加入 PyInstaller、DMG 或 installer CI。
- 使用者要求 commit 分批；本輪已授權繼續實作與分批 commit，後續已授權直接推至 main；部署、付費訂閱與代操作 Yahoo 未獲授權。
- Yahoo 維持唯讀，建議由使用者在 Yahoo 手動執行。不得為了測試直接變更真實聯盟。

## 2. 接續前一批修正與審查

| Commit | 本輪範圍 |
| --- | --- |
| `2334ec4` | IL 回歸與明確換人共用按日期排序的事件時間軸；先 IL 必要釋出／啟用，再執行同日換人。完整合法後續檢查包含再取得、保護、容量、鎖定延期與多名回歸。冷啟動 Today 驗證並沿用序列化 F3 的前後 IL 決策，不再搜尋 ROS；釋出後重新取得的球員有最終排陣動作，同日連續換人保留原順序。共 12 檔，含回歸測試。 |
| `88c96cb` | 預測內容身分與保存計畫加入完整聯盟規則雜湊；已知規則不符時，Today 與所有原始回顧決策明確拒絕套用。目前無雜湊的舊歷史仍相容讀取，但不能重用為 Today 計畫，也不能證明其當時規則。共 6 檔，含回歸測試。 |

前一輪本機四批為 `868df63`（公式）、`f4b5f14`（預測與季賽安全契約）、`7e0d8cb`（畫面）、`a4f7bfe`（benchmark 與文件）。本輪基準為 `a4f7bfe`；程式兩批合併後 HEAD 為 `88c96cb744e1520b5f17ab33c63230252b25d1d6`。文件更新另批提交，提交序列以 `git log` 為準。沒有 push。

接續前一批的驗證與獨立審查：

- 原生 macOS `scripts/verify.py`：exit 0，**1,127 passed／260.64 秒**，JS 語法、6 個匯入契約、Ruff、220 檔格式、型別、vulture、deptry 通過。
- Goldband 複審 `df550959-264c-4718-9b00-481366b2d354`：兩項 required evidence 均 fresh verified-pass，0 failure／coverage gap／runtime incomplete；隔離完整測試 **1,127 passed／288.38 秒**。鎖定 Python 3.13.7 與依賴、唯讀容器、UID 65534、隔離網路已實際稽核。
- 本輪初審 `4e7b7e31-5147-467d-8a86-b7cee3765bc6` 發現 S-001（重新取得 IL 釋出球員後漏排陣）。已修正；相關缺漏與同日換人反序均先重現失敗，再修補通過。正式複審確認 S-001 closed、contract preserved、`prior-blockers-open=false`、`deterministic-contract-complete=true`、`closure-complete=true`、`completion-authorized=true`。
- 報告仍保留 `no-new-findings=false` 與初審 finding 的歷史，不能改稱「零 findings」。此結論只涵蓋目前程式候選與本機／隔離證據，不是整份規格驗收。
- 受審候選 digest：`f9985729b708e014e167a70ee8625ef3601555d5fcbdda0f23a50de96341df91`。程式分批提交後 **290 檔、0 drift**；文件更新發生在複審後。中間 commit 未各自重跑完整驗證，沒有宣稱每批各自完整驗收。
- 合成資料的實際 Chrome 已讀回 IL／F3 合併操作、S-001 修復，以及美東 DST 春／秋轉換與台北顯示。沒有呼叫真實 Yahoo。另一個同日連續換人 UI 案例因測試資料顯示 Yahoo 授權失效而未完成；單元回歸已通過，瀏覽器該條仍待驗，不把 fixture 阻塞當成已確認的產品缺陷。
- 本次驗收伺服器皆已經由「結束助手」關閉，程序 exit 0；沒有留下本輪測試服務。

最新本機原始證據（不在 clone 中；暫存可能清除）：

- `/Users/leo/.goldband/workflow-runs/artifacts/df550959-264c-4718-9b00-481366b2d354-code.md`
- 同目錄 `df550959-264c-4718-9b00-481366b2d354-review-closure.json`
- 同目錄 `4e7b7e31-5147-467d-8a86-b7cee3765bc6-f95beee4-review-evidence.json`
- `/private/tmp/fba-remediation-report/nonperf-closure-native-verify.log`
- `/private/tmp/fba-remediation-report/nonperf-closure-file-sha256.json`
- `/private/tmp/fba-remediation-report/nonperf-full-scope-formula-candidates.json`（F1 算術盤點線索，非語意完整性證據）
- `/private/tmp/fba-remediation-report/nonperf-browser-verification.json`，另有 `nonperf-ui-goldband-s001-fixed.txt`／`.png`；均為合成環境補充證據。

### 2026-09-30 第一批歷史修正

| Commit | 範圍 |
| --- | --- |
| `e3c8a68` | 共用 Windows socket：exclusive/reuse 衝突、10013／10048 重試；附模擬 socket 測試。 |
| `b560b26` | Yahoo `ST` → 內部 `STL` 對照；既有本機 catalog 也取得新標籤，保留使用者覆寫。 |
| `f2bf74b` | 預測、模擬、建議、擬合、UI 與新舊回顧計分相容性修正，詳下表。 |

這三批是同一個已審查候選的拆分；完整測試證據對應合併後內容，**沒有宣稱每個中間 commit 都各跑完整驗證**。
當時 clone 交付 commit 為 `63749e1`，當時交接文件另批提交，沒有 push。

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

### 2026-09-30 第一批歷史驗證與審查

- 最終候選 digest：`66268d101495970fddfba232fb7ea21a9e62fda2088a3008536ecccdcb525d3e`。
- Goldband 完整 `scripts/verify.py`：**753 passed，261.96 秒**；包含 JS 語法、lint、format、型別、依賴、死碼、匯入邊界與測試。
- 執行範圍是鎖定 Python 3.13.7 的隔離 Linux 容器，4 CPU／4 GB；不是 Windows 實機或真實供應商驗收。
- 本機另有 macOS socket 綁定／重啟／HTTP 測試；sandbox 內 bind 曾被禁止，移至允許 loopback 的環境重跑 6 項通過。不是 Windows 證據。
- 初審 `1a6bcd1c-5b98-4c78-8113-830ea62eed18` 發現 S-001：新平手語義會扭曲舊預測 Brier。
- 複審 `bb37bb13-d702-49ad-b014-7ef0b9f472b1` 確认 S-001 已關閉，`closure-complete=true`、`prior-blockers-open=false`，兩項 deterministic evidence 通過。
- 報告同時保留 `no-new-findings=false`，**不要改寫成「零 findings」或「所有使用者回報已關閉」**。
- 更早一次 `adf9a01d-01e9-4d21-b2bf-7d8eefcda47e` 因審查期間候選變動而中止，不能當成通過證據；上面兩輪才是有效的初審／複審。
- 當時分批 commit 沒有修改受審查的程式內容。當時新增交接文件沒有另開程式審查，也沒有因此重跑整套測試。

本機證據（不在 clone 內容內，轉交另一台電腦前需另行安全保存）：

- `/Users/leo/.goldband/workflow-runs/artifacts/bb37bb13-d702-49ad-b014-7ef0b9f472b1-code.md`
- 同目錄 `bb37bb13-d702-49ad-b014-7ef0b9f472b1-review-closure.json`
- 同目錄 `1a6bcd1c-5b98-4c78-8113-830ea62eed18-bea80021-review-evidence.json`

### 歷史效能證據（目前暫停，不是最新候選計時）

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

2026-10-02 實際 main CI：`Check` 36912989004 的 Linux／macOS 完整檢查均成功；`Inseason source` 36912989052 的 Windows 有 33 failed／926 passed／232 errors，另外兩個 job 被矩陣取消，V5 未通過。已確認測試 fixture 的 artifact 路徑誤用平台分隔符，以及 Windows checkout 的 CRLF 使 C++／JS 原始 SHA 改變（本機 LF→CRLF 的 SHA 與 CI 完全相符）。修正 fixture 使用既有 POSIX 契約，新增 LF checkout 規則，保留 raw-byte SHA 檢查。另停止矩陣失敗連帶取消，暫加 Windows 原生 DLL imports／venv launcher PID 診斷；沒有跳過任何完整檢查。DLL 載入、PID 測試與開啟中編輯器的檔案替換仍待 Windows 證據與修正，不能算平台結案。

狀態用語：「本機已修」涵蓋程式與合成驗證；「未驗」保留尚未取得的實際來源、平台或品質證據，不將合成結果外推。
下表追蹤完整驗收條件；「本機已修」不等於真實來源或整份規格結案。現況已更新至本輪，效能項目暫停。原始案例未提供，已自行建立合成驗證，不再等待不存在的原始資產。

表內程式位置若省略根目錄，Python 模組均相對於 `src/fba/`；測試相對於專案根目錄。

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
| B2 | 小整週空間有聯合 exact oracle；大空間仍為 coordinate，品質未全面驗證。 | `inseason/matchup.py::optimize_total` | 取得反例，比較整週排陣品質及規格要求的每日精確解；所有 F3/F4/F5 共用修正後結果。若用近似策略，須清楚說明並有品質證據，不能稱全域最優。 |
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
| M5 | 本機已修 IL／未來換人合併事件、完整合法後續、容量／保護／鎖定延期、再取得與冷啟動 Today 沿用；本批已補同日連續換人的合成 Chrome 流程。多 IL 仍逐次估值，未證明聯合最優；真實 Yahoo 未驗。 | `inseason/matchup.py::roster`、`inseason/season.py`、`inseason/today.py` | 包含預計回歸、移出 IL、容量、保護及必要丟人的合法轉換；與未來換人共用時間軸，動作與條件排陣一致，不把球星 ROS 當零。合成 oracle 不能替代真實來源與完整搜尋品質驗收。 |
| M6 | 類別／整週參數、匯出及擬合分開本機已修；holdout 未完成。 | 參數契約／defaults、`inseason/matchup.py`、`inseason/review.py::calibration_history/refit_history`、backtest | 分開類別 0.82／整週 0.80 的規格初值及出處；匯出兩層級的預測與結果，分開擬合、展示與樣本外驗證。舊版積分不可當新整週勝率訓練資料。 |
| M7 | 自建接受模型案例、分離／不識別檢查及求解已驗；真實提案機率未校準。 | `formulas/fitting.py`、`inseason/operations.py::refit_acceptance`、保存的 acceptance-fits | 自行建立正常、分離與不識別案例，驗證係數／需求方向、殘差、log loss、顯示與既有報告處理；真實提案樣本另作驗收。不要把合成訓練擬合說成真實接受機率校準。 |

### D. 回顧與回測

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| R1 | 固定 origin／去重／原始歷史／類別 cohort 已修；本輪補完整規則雜湊，檢查所有原始決策並拒絕已知不符。無雜湊舊紀錄可相容讀取，但其歷史規則出處未驗。 | `inseason/operations.py::week_result`、`inseason/review.py::weekly_review/cumulative_review` | 定義固定評估 cohort／去重鍵；同一資訊重開頁不改變權重，保留不同決策與原始歷史。完整規則變更不能去重為同一預測或套用現在規則重評；缺歷史規則出處應明示限制。 |
| R2 | 同步成功後保存當時預測、內容去重／失敗隔離本機已修；不補造過去預測。 | `inseason/session.py` 同步完成路徑、`inseason/operations.py::complete_reviews` | 不依賴頁面瀏覽的當時預測保存；同步、排程、重啟不重複寫入。不准用事後資料補造事前預測。 |
| R3 | 分群樣本量、不等權重 n_eff 與提醒有獨立公式答案；真實區間 coverage 未驗。 | `inseason/review.py::calibration_bins/weekly_review/cumulative_review` | 明確樣本量／不確定性政策與參數出處，覆蓋少量與足量案例；不是直接提高門檻或隱藏提醒。 |
| R4 | 未完成：合法凍結時間序列、實際引擎因果重播、跨季 holdout、策略比較與 recall。 | `inseason/backtest.py`、`contracts/inseason_backtest.py`、`inseason/replay.py` | 用真實凍結時間序列跑實際引擎，前季訓練／後季 holdout；5/10/20/30/40 場預測誤差、旗標命中率、出賽 Brier、10 分箱≤5pp、規格 Brier 基準。換人 recall≥95%、與不動／公開排名策略比較也須真實因果重播。 |

### E. 畫面、公式與參數

| ID | 現況／後果 | 接手位置 | 完成條件 |
| --- | --- | --- | --- |
| U1 | 本機已補對手／時間／場次／基準／raw 機率；真實聯盟整合未驗。 | `apps/inseason/static/views.js`、結果契約 | 明確基準線與所選週資料，不和「無手調」基準混淆；數字可追溯且經瀏覽器核對。 |
| U2 | 本機合成 Chrome 已驗忽略／忽略到期、傷情改變解除舊忽略、手調到期恢復、新球隊頁／舊手調提醒及接受／撤銷；供應商設定差異恢復仍待真實 Yahoo。 | UI、projection flags、同步設定差異 | 依規格整理待確認事項、來源／理由／操作，核對忽略／到期／狀態改變後行為。 |
| U3 | 本機完成 20 數字獨立手算、實際手調／撤銷、UI 與模擬值同源；合成資料邊界。 | `apps/inseason/static/views.js::playerCard`、`inseason/projection.py::blend_player/effective_projection` | 清楚分開先驗、混合、手調及最後由命中率重算的 made；抽 20 個 UI 數字獨立手算（至少 3 位小數）並驗證傳給模擬的值。 |
| U4 | 合成資料的實際 Chrome 已驗跨日、IL／F3 合併、DST 春秋、S-001、新舊回顧與提案更新；本批另完成同日連續換人的有效合成授權／保存計畫沿用流程；真實 Yahoo 未驗。 | today／trade／review UI | 聯盟美東日期、使用者台北顯示、DST、跨午夜、交易內容及舊版 Brier 標籤都實際操作讀回。 |
| F1 | 129 條登錄；142 檔／464 Python 候選及 C++／JS 人工盤點 pending 均為 0。兩應用隨機 20 個實際 scalar trace 已手算／展開核對；拍賣候選已審查／提交；逐功能核對再補四組季賽 trace，該新候選完整 1,193 項測試及獨立審查已通過，公式所有權／UI trace 本機範圍結案；不包含原規格功能 F1 的歷史校準。 | `formulas/*`、`design/formula-ownership.json`、相關 tests | 全範圍語意所有權與登錄一對一；業務公式只在 formulas，編排／搜尋／驗證有具體理由；UI trace 同源。檢查不能自動證明語意正確。 |
| F2 | 124 參數葉值逐鍵核對，初值無不符、出處已修；實證訓練／驗證對應仍未完成。 | defaults/parameters.json、回測報告 | 逐鍵核對初值、來源、訓練／驗證季與限制；已找到原規格指定的 `/Users/leo/fantasy-research-2026-27/claude-report-v2/evidence-2026-09-27/` 程式／JSON，重播 318 位球員、1,446 案例與原結果在 1e-12 內零差異。原研究是同季擬合，ESPN dump 於 2026-09-25 季後擷取，不能證明歷史公開時點；也不能當目前 strict-win 引擎的跨季校準。無證據不能改標已校準；OREB 等未回測項另列。 |
| F3 | 10 隊、不同位置與 OREB／A/T／H2H Each Category 的合成 F0–F6 本機已有驗證。 | `tests/inseason_support.py`、`test_inseason_portability.py`、跨功能測試 | 真正改類別公式（含比率／A/T）、位置、10 隊、H2H Each Category，F0–F6 跑完且合法；只改設定，不在程式塞聯盟特例。 |

### F. 交付與實際環境

| ID | 未完成的驗收 | 完成條件 |
| --- | --- | --- |
| V1 | 未完成：真實 OAuth／refresh／撤銷／限流／時效／設定差異恢復。 | 使用授權測試帳號，安全保存錄製 fixture；驗證只讀、來源時間、失敗訊息及恢復。合成資料或 mock token 不算。 |
| V2 | 未完成：原生 Windows socket／共存／中斷復原。 | 真實 Windows 11 驗證 socket 互斥／保留 port、10013 換 port、單一實例、同時雙開、正常重啟、強制終止後 5 秒內無殘留、寫入中斷復原；共用 runtime 也要驗證競標桌與季賽助手共存。 |
| V3 | 未完成：無 Python 的乾淨 macOS／Windows clone 與自己的正式資料／憑證。 | macOS／Windows 無 Python 環境依 README 安裝必要的 uv、由 uv 取得鎖定 Python 後雙擊啟動；中文／空白路徑、自己的憑證、首次資料取得／同步。先前 macOS 副本測試機已有 Python 3.13.7，不算無 Python 證據。 |
| V4 | 實際 Chrome 的正常結束已驗；命令檔雙擊第二次、卡住／強制終止等全部情境未驗。 | clone 命令檔實際雙擊第二次時可見診斷，不偷開另一實例。原「打包版沒有結束選單」已被 clone 交付取代；仍要驗證現有「結束助手」、關分頁及命令視窗的行為。 |
| V5 | 未完成：三平台實際完整 CI 與跨平台數值一致，不以 YAML 或單機 fixture 代替。 | 真正執行 macOS／Windows／Linux 完整工作流程；同快照、帳本、參數與 seed，排序相同、數值差≤1e-9。CI YAML 存在、單機 golden fixture 更新都不算跨平台通過。 |
| V6 | 未完成：原規格逐條最終整合驗收與里程碑，不因本機審查通過而結案。 | 逐條重查原規格，尤其跨功能手調／撤銷、因果性、錯誤可見、秘密掃描、公式手算、設定可替換、來源時間與日誌；附真實環境證據、未測項與實際 p50/p95，再開獨立審查。 |

## 5. 已確定的決定與外部證據缺口

1. 使用者已選擇**只接受免費且條款允許的來源**；付費訂閱不在範圍內。尚未取得完整且權利適用的 NBA／公開排名 adapter，不把自訂 JSON 或網站爬取假裝正式入口。
2. 原缺陷腳本、30 組排陣與 40 組接受模型沒有提供；使用者要求自行驗證。原始 2025–26 研究資產已在本機找到並重播，5 份 ESPN players／schedule 檔雜湊與 manifest 一致。現有資料是季後擷取，仍缺當時的傷情／排名／先驗公開時間及目前引擎的跨季 holdout；不能把 `known_at` 倒填成比賽當日。
3. 使用者已送出 Yahoo API 申請，等待審核／核發；真實存取資格／去識別錄製回應尚未驗證。帳號、付款與 Yahoo 操作不在本輪授權內；不要求把 token 放入 repo、日誌或交接文件。

公式盤點與白箱本機驗收已完成。搜尋品質、真實來源與尚未涵蓋的交付平台仍有後續工作；不能以外部資料缺口概括所有未完成項目。

## 6. 建議接續順序

1. 核對 `git status`、本文件及最新程式／文件 commit；不要沿用第一批的 753 或前一輪的 1,093 項測試當作最新候選結果。
2. E/F1 全範圍公式所有權及 UI trace 本機驗收已完成，不重做未變的候選。後續新增數值業務須在既有 owner 補登錄／獨立答案／盤點；沒有實證不調整模型參數。
3. 繼續 A1/A2/A6/A7 與 M3/V1 的正式資料來源／Yahoo 證據；遵守免費、條款允許及唯讀。同日連續換人的合成 UI 已補；既有 IL 合併與規則變更回歸不需重做未變的測試。
4. 取得合法歷史後執行 R4/F2 holdout／因果策略回測；執行 V2–V6 的原生 Windows、無 Python 乾淨 clone、跨平台實際 CI 與完整整合驗收。未校準項保留 `Assumption`，不以合成訓練擬合冒充樣本外品質。
5. 效能工作依使用者指示暫放；B1–B6 不因暫停而結案，也不為本輪審查降低抽樣、刪候選或放寬產品 budgets。大型整週與 F3 shortlist／beam 的品質缺口保留。
6. 新程式變更需相符的驗證與 Goldband 獨立審查，再分批 commit；目前已獲 main 推送授權；部署仍需另取得授權。

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
本輪複審 manifest 位於 `/private/tmp/fba-remediation-review/nonperf-closure-contract.json`，初審為同目錄 `nonperf-initial-contract.json`；這是本機暫存資產，接手須核對是否仍存在、image／lock hash 及需求是否適用。
正式 closure 的 `--closure-artifact` 使用初審 evidence JSON，不是 closure JSON；本輪為 `4e7b7e31-5147-467d-8a86-b7cee3765bc6-f95beee4-review-evidence.json`。審查期間固定候選；之後新增程式範圍應重新建立契約與相符證據，不能沿用本輪綠燈。
不要繞過 sandbox、auto-review、pre-commit 或 Goldband lineage。缺真實平台證據就明說，不將 gate 綠燈冒充完整交付。
