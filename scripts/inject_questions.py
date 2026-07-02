"""
inject_questions.py
把 quiz_json/ 內所有有效 JSON 題庫直接嵌入 quiz.html，
讓網頁第一次打開就自動寫入 localStorage，不需要手動匯入。

重複執行安全：舊的嵌入段會被新的取代（不影響做題紀錄）。
"""
import json, re, sys
from pathlib import Path

BASE     = Path(r"D:\一階國考\歷屆")
JSON_DIR = BASE / "quiz_json"
HTML     = BASE / "quiz.html"

MARKER_BEGIN = "// ──[AUTO-EMBED-BEGIN]──"
MARKER_END   = "// ──[AUTO-EMBED-END]──"

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # ── 讀所有有效 JSON ───────────────────────────────────────────────────
    embedded = {}
    for jf in sorted(JSON_DIR.glob("*.json")):
        data = json.loads(jf.read_text(encoding="utf-8"))
        if not data.get("questions"):
            continue
        exam    = data["exam"]
        year    = data["year"]
        session = data["session"]
        id_     = f"{exam}_{year}_{session}"
        embedded[id_] = {
            "exam":     exam,
            "year":     year,
            "session":  session,
            "questions": data["questions"],
        }
    print(f"共讀入 {len(embedded)} 份題庫")

    # ── 產生嵌入腳本 ──────────────────────────────────────────────────────
    ed_json = json.dumps(embedded, ensure_ascii=False, separators=(',', ':'))

    inject = f"""{MARKER_BEGIN}
// 自動嵌入題庫（由 inject_questions.py 產生，共 {len(embedded)} 份）
(function(){{
  var ED={ed_json};
  var raw=localStorage.getItem('nqkb_sets');
  var sets=raw?JSON.parse(raw):{{}};
  var n=0;
  for(var id in ED){{
    var d=ED[id];
    var qKey='nqkb_qs_'+id;
    var nextQs=JSON.stringify({{questions:d.questions}});
    var nextMeta={{id:id,exam:d.exam,year:d.year,session:d.session,total:d.questions.length}};
    if(localStorage.getItem(qKey)!==nextQs){{
      localStorage.setItem(qKey,nextQs);
      n++;
    }}
    if(JSON.stringify(sets[id]||{{}})!==JSON.stringify(nextMeta)){{
      sets[id]=nextMeta;
      n++;
    }}
  }}
  localStorage.setItem('nqkb_sets',JSON.stringify(sets));
}})();
{MARKER_END}"""

    # ── 讀 HTML、替換或插入嵌入段 ─────────────────────────────────────────
    html = HTML.read_text(encoding="utf-8")

    # 已有嵌入段 → 替換
    pat = re.compile(
        re.escape(MARKER_BEGIN) + r".*?" + re.escape(MARKER_END),
        re.DOTALL
    )
    if pat.search(html):
        new_html = pat.sub(inject, html)
        action = "取代舊嵌入段"
    else:
        # 沒有 → 插在 showView('home'); 前面
        target = "showView('home');"
        if target not in html:
            sys.exit(f"找不到插入點 '{target}'，請手動確認 quiz.html")
        new_html = html.replace(target, inject + "\n" + target, 1)
        action = "新增嵌入段"

    HTML.write_text(new_html, encoding="utf-8")
    size_kb = len(new_html.encode("utf-8")) / 1024
    print(f"[{action}] quiz.html 已更新 ({size_kb:.0f} KB)")
    print(f"題庫涵蓋：{sorted(embedded.keys())[:3]}... 等 {len(embedded)} 份")

if __name__ == "__main__":
    main()
