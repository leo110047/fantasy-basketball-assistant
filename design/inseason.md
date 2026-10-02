# 季賽助手

季賽助手是獨立本機應用，入口為 `fba-inseason` 或 `fba inseason`。
Yahoo Fantasy 的名單、交易、排陣均由使用者在 Yahoo 操作；本程式的 Fantasy API adapter 只有 GET。

## Clone 與啟動

1. 安裝 Git 與 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
2. 執行 `git clone https://github.com/leo110047/fantasy-basketball-assistant.git`。
3. 進入專案資料夾，macOS 雙擊 `啟動季賽助手.command`，Windows 雙擊 `start-inseason.cmd`；Linux 或終端機使用：

```sh
uv run --locked --no-dev fba-inseason
```

命令檔會切換到自身所在的專案目錄。首次啟動由 `uv` 依 `.python-version` 準備 Python 3.13.7，
依 `uv.lock` 建立 `.venv` 並安裝執行依賴，完成後自動開啟本機網頁。
不必另外安裝 Python；第一次需要連網下載，缺少 `uv` 會顯示安裝指引。
後續啟動沿用既有環境；鎖定檔與專案設定不一致會明確失敗，不在啟動時重寫鎖定檔。
季賽助手的一般啟動不需要 Node.js、C++ 編譯器或開發測試套件。

更新前先從網頁「結束助手」，執行 `git pull --ff-only` 後再啟動；`uv` 會同步新版本的依賴。
原始碼和命令檔要一起保留，`.venv` 可重新建立，使用者資料不存放在專案內。
隔離資料或開發測試可使用 `--data "/path/中文 資料"`，不開瀏覽器時加 `--no-browser`。
預設資料位置由 `platformdirs` 決定：macOS 的 Application Support、Windows 的 AppData/Roaming。

交付方式依使用者要求改為 clone 原始碼，取代原規格中的安裝檔要求。
CI 的 `Inseason source` 工作保留三平台完整 `scripts/verify.py`（含既有競標回歸）、啟動入口與數值容差檢查，
僅上傳數值比較報告。定義工作流程不等於已取得遠端執行結果。

## Yahoo 首次設定

1. 在 [Yahoo Fantasy API 存取頁](https://sports.yahoo.com/developer/access/) 申請存取，等待 Yahoo 審核／核發 Client ID；若需建立或調整應用，依核准通知於 Yahoo Developer Network 操作。
2. 使用 Fantasy Sports 唯讀權限；授權流程採 [OAuth authorization code](https://developer.yahoo.com/oauth2/guide/flows_authcode/) 的 `oob` 回傳。
3. 在「資料與同步」輸入 Client ID、Client Secret，開授權頁後貼回授權碼。憑證只寫入作業系統憑證庫，無明文檔案 fallback。
4. 選擇本季聯盟並同步。核對草稿中的規則，特別是時區、IL 狀態、鎖定時間、生效日及平手規則，確認後才採用。
5. 設定已鎖定的 `forecast.json` 路徑、SHA-256、公開時間，及已取得授權的 NBA JSON 資料服務。
6. 完成 Yahoo player key 到內部 ID 的對照。隊伍名單仍有未對照球員時，聯盟功能會停用；未對照自由球員會列出並排除。

正式 NBA 供應商尚未選定。現有 `AuthorizedFeed` 接受供應商明確授權的 HTTPS JSON，契約是 `PlayerSnapshot`，不是通用 NBA 網站網址。
它包含有 `known_at` 的球員身分、逐場 box score 與賽程修訂。不得把研究用 ESPN 快照當成已授權的正式來源。
**目前沒有可直接接上現成 NBA 供應商的轉接器。一般使用者 clone 後，無法只填入供應商網址就使用 F1–F6。**
除非自行提供符合專案格式的資料服務，否則球員資料、球員對照與所有分析功能都無法完成初始化。
尚無實際 Yahoo 授權與錄製回應；`tests/fixtures/yahoo-example.json` 明確標示為合成結構測試，不能用來宣稱真實同步已通過。

F1/F2 可以不連結 Yahoo，但仍需上述 NBA 資料服務與賽季前預測；目前不是 clone 後即可使用的離線功能。
資料備齊時保存在獨立的球員工作區，聯盟歸屬留空，F3–F6 停用。選擇聯盟後使用該聯盟的資料目錄與帳本。

## 日常操作

- 「球隊與手調」顯示有效預測、球員卡與算式。保存前先看預覽；同組調整一起建立、一起撤銷。撤銷會新增紀錄。
- 「每週對戰」計算 Yahoo 已知總量與剩餘逐日合法先發，並列出原始、收縮後機率及常態近似。
- 換人建議不以回測報告鎖住執行。未通過獨立賽季驗證時顯示未校準提示；預設參數含假設，不能宣稱已通過交付驗收。
- 「交易」支援手動組合、搜尋、不等人數補位及提案結果紀錄。接受機率在提案資料擬合前標示「未校正」。
- 「今日」列出每日合法先發、鎖定、IL 回歸與相反排陣的本週影響；換人待辦沿用有效的保存 F3 計畫，不在這頁逐人重跑整季移除搜尋。
- 每週最後比分到齊後，下一次同步產生回顧；已記錄的預測不因新資料改寫。
- 新預測的整週勝率只計贏的類別多於輸的類別。舊版含平手積分的預測依原契約評分，顯示為「舊版積分 Brier」，不混入純勝率的累積整週 Brier。
- 傷兵的未來預估回歸日會用於未來可出賽估計；已到期或過期的估計不覆蓋最新傷兵狀態。這不是回歸已確認或傷病預測已校準的證據。
- 關閉分頁會繼續同步；用頁首「結束助手」停止程式。

## 設定與資料

`preferences.json` 是 port、同步間隔、時區、資料時效、保留加人額度、λ 與不可交易清單。
`parameters.json` 每個模型數值附出處，`yahoo-catalog.json` 是 provider stat/position 對照與待確認的規則草稿。
聯盟資料分別存於 `leagues/<league-key-hash>/`；身分對照共用一份 `identities.json`。

`snapshots/` 是內容定址的不可變原始層；索引與當前狀態用同目錄暫存檔、fsync、原子改名發佈。
`ledger.json` 是當前追加式手調帳本，`ledger-history` 保留各版。有效預測由純函數產生，不另存一份可修改的球員預測。
`operation-log` 記錄操作、資料雜湊、參數版本、耗時及成敗；不記錄 OAuth 內容。
鎖檔中的 token 是本機每次啟動的隨機權杖，不是 Yahoo 憑證；勿分享鎖檔或完整啟動網址。

設定差異會停用 F3–F6，直到選擇匯入或保留。資料過期或授權失效也會停用聯盟功能；既有球員來源仍可供 F1/F2 使用。
多人同時修改同一本帳本時，請求必須附讀取時的雜湊，舊分頁會收到明確衝突訊息。

## 換季與回測

1. 保存上一季的原始快照、手調、預測及提案紀錄。
2. 產生新一季賽季前預測並記錄 SHA-256。
3. 在 Yahoo 聯盟選擇器重新選擇本季聯盟，匯入並確認新規則；不沿用舊聯盟 key。
4. 設定新賽季球員資料來源，更新身分對照。
5. 用前一季訓練、下一季驗證，執行：

```sh
uv run --locked fba inseason-backtest /path/study.json \
  --parameters /path/parameters.json --output /path/validation.json
```

輸入契約為 `BacktestStudy`，包含兩季凍結資料與帳本、檢查點、候選 k／半衰期／產出旗標門檻，以及分離的訓練／驗證對戰觀察。
出賽紀錄須明列每場賽前狀態的公開時間、開賽時間、是否出賽與結果公開時間；不把缺少 box score 自動當成缺賽。每種設定狀態都必須有訓練與驗證觀察。
出賽機率使用前季相同狀態的實際出賽比例；產出旗標門檻以「標記後採用近期值」的前季未來誤差選擇，同誤差取較保守的高門檻。驗證季不參與選擇。
報告包含擬合參數及雜湊、分鐘／命中率／每分鐘數據的驗證誤差、各旗標命中率、各狀態出賽 Brier、門檻候選比較與校準分箱。沒有被標記的樣本時，命中率是空值，不能宣告門檻驗證通過。
資料不足、空校準區間或任何門檻失敗均不算通過。F3 的篩選召回率與策略重播仍需各自的歷史資料驗證。

## 維護與驗證

```sh
scripts/check
```

安裝完鎖定依賴後，`uv run --no-sync python scripts/verify.py` 可離線執行門檻。
開發者先執行 `uv sync --locked` 安裝包含開發工具的依賴。完整檢查另需 Node.js 與 C++ 編譯器；一般季賽助手使用者不需要。
檢查包含 ES module 語法、lint、型別、死碼、依賴、純計算層邊界與測試。
合成資料測試、macOS 本機執行、Windows CI 與真正 Yahoo API 是不同證據範圍，不可互相替代。

主要契約位於 `contracts/inseason*.py`；Yahoo wire 格式只在 `data/yahoo*.py`。
既有數學模組已移到 `formulas`，競標流程移到 `auction`，預測流程移到 `projection`。
scalar 與向量公式登錄由 `formulas/registry.py`、`formulas/arrays.py` 提供，兩應用共用 `runtime/static/formulas.js`。目前包括預測混合、估值、市場／停損、bootstrap 校準、共變異數、多數類別分數與邊際權重；完整既有公式盤點仍未通過全部驗收。

### 換人策略的歷史重播

```sh
uv run --locked fba inseason-replay /path/replay-study.json \
  --parameters /path/parameters.json --output /path/policy-report.json
```

`PolicyReplayStudy` 每個案例含當時的 Yahoo 名單／比分、球員快照、先驗、帳本、決策時間，
以及分開的賽週最終球員數據與 Yahoo 最終比分。未來結果只供計分，不會傳入選人或排陣。
每週比較不動、照公開排名撿人、照建議；同時完整評估合法的一次加人候選與額度內的多次加人序列，分別量測快速篩選和 beam search 召回率。
案例重複、未來的決策快照、未完賽或缺少公開排名會明確拒絕。樣本不足不會標示驗收通過。
這是從相同週初名單開始的逐週配對試驗。`oracle_max_plans` 限制窮舉規模；超過就拒絕產生通過結果，不能將截斷搜尋當成完整標準答案。正式召回率與策略成效仍須相符的歷史資料。

### 用保存的預測重新擬合

在「每週回顧」按「匯出下一季校準輸入」，可直接執行：

```sh
uv run --locked fba inseason-calibrate /path/calibration-history.json \
  --output /path/calibration-fit.json
```

輸入保留原始預測機率、事後實際結果、預測時間與快照雜湊；不重新計算舊預測。
輸出有 c、出處、樣本數與訓練 Brier。這份擬合報告不取代獨立賽季的 holdout 關卡，
不能用來宣稱換人建議已通過交付驗收。

### 目前仍未滿足的完整交付條件

完整待辦、已修正範圍、證據限制及接續步驟以 [交接文件](inseason-handoff-2026-09-30.md) 為準。本機測試與獨立審查不代表真實來源、Windows 或整份規格通過。

- 正式 NBA／公開排名來源與真實 Yahoo OAuth／錄製回應尚未取得。使用者已送出 Yahoo API 申請，等待審核／核發。
- 公式 owner 已持續抽取與盤點；拍賣 JavaScript 及最終 UI trace／全產品隨機手算仍待完成，見 [公式所有權](formula-ownership.md)。
- 原研究資產已重播；新跨季單欄位診斷仍不是因果引擎回測。傷情、當時排名／先驗公開時點、旗標、出賽、校準及策略的真實 holdout 未通過。參數保留未校準／Assumption 標示。
- 整週排陣使用完整搜尋與保守上界剪枝，只有完成搜尋才發布結果；逾時明確失敗。合成四天／六天排陣已與完整窮舉比對，top-10 交易也有小規模 oracle；F3 篩選／beam 的真實召回率仍未驗證。對手保持固定名單及合法基準排陣，不模擬串流反應。
- ROS 抽樣、共同亂數、跨請求失效與保存計畫沿用已有本機證據；效能驗收依使用者決定暫停，樣本、候選與 budgets 不降低。
- 同步比分進度與更正仍需實際 Yahoo 核對；不猜歷史先發、不用事後資料補造事前預測。
- 原生 Windows、無 Python 的乾淨 macOS／Windows clone、三平台實際 CI／數值比較及完整逐條整合驗收未完成。
