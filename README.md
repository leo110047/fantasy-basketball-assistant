# Fantasy Basketball Assistant

設定驅動、每年重用的本機 Yahoo 拍賣籃球助手。

目前為階段 0：只有設定 schema 與資料／介面型別草案，尚無選秀功能。

- `design/schemas/`：league、season、model 與匯入資料的欄位契約。
- `design/contracts.pyi`：核心、快照、選秀與季中共用介面。

聯盟規則不得補預設；計算只讀凍結快照；核心不做 I/O。每個階段結束停下交付，每次 commit 前需通過 Goldband 獨立審查。
