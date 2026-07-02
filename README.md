# 1NationalExam

一階國考刷題網站與必要資源。

## 內容

- `index.html`
  - 可直接開啟的單檔刷題網站
  - 已內嵌 62 份題庫資料，可直接使用
- `quiz_json/`
  - 原始題庫 JSON（醫學一、醫學二，100 年到 115 年第一次）
- `scripts/`
  - `pdf_to_quiz_json.py`：由題目 PDF 與答案 PDF 重建題庫
  - `verify_quiz_json_against_source.py`：重新比對題庫與原始 PDF
  - `inject_questions.py`：將題庫嵌入網站
- `reports/`
  - 最新驗證報告

## 驗證狀態

目前題庫驗證結果：

- 總檔數：62
- 完全通過：62
- 題數不一致：0
- 題幹不一致：0
- 選項不一致：0
- 答案不一致：0
- 空答案：0

## 使用方式

直接開啟 `index.html` 即可。

若要重新產生題庫，`scripts/pdf_to_quiz_json.py` 需要 `PyMuPDF`。
