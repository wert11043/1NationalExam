"""
verify_quiz_json_against_source.py

嚴格比對 quiz_json 與原始題目 / 答案 PDF：
  1. 題數、題號、題幹、選項是否與原始題目 PDF 重新解析結果一致
  2. 答案是否與原始答案 PDF 一致
  3. 更正答案 / 多答案給分 / 一律給分題是否被 JSON 正確表示
  4. 額外抓結構性污染，例如頁碼殘留、私用字元、選項數異常

輸出：
  - 歷屆/quiz_json_verification_report.json
  - 歷屆/quiz_json_verification_report.md

用法：
  python verify_quiz_json_against_source.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from pdf_to_quiz_json import (
    extract_text,
    fullwidth_to_ascii,
    parse_answer_metadata_from_pdf,
    parse_questions_from_text,
)


BASE = Path(r"D:\一階國考\歷屆")
JSON_DIR = BASE / "quiz_json"
REPORT_JSON = BASE / "quiz_json_verification_report.json"
REPORT_MD = BASE / "quiz_json_verification_report.md"

SESS_SHORT = {"第一次": "第1次", "第二次": "第2次"}
HEADER_RE = re.compile(r"(?:代號[:：]\s*\d+|頁次[:：]\s*\S+)")
PUA_RE = re.compile(r"[\uE000-\uF8FF]")
CHOICE_MARK_RE = re.compile(r"[]")
QUESTION_MARK_RE = re.compile(r"^\s*\d{1,3}\s*$")


def parse_declared_question_count(q_text: str) -> int | None:
    text = normalize_text(q_text)
    patterns = [
        r"本科目共\s*(\d{1,3})\s*題",
        r"單選題數[:：]?\s*(\d{1,3})\s*題",
        r"題\s*數[:：]?\s*(\d{1,3})\s*題",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None


def normalize_text(s: str) -> str:
    s = fullwidth_to_ascii(s or "")
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_source_paths(exam: str, year: str, session: str) -> tuple[Path | None, Path | None]:
    q_dir = BASE / exam / "題目"
    a_dir = BASE / exam / "答案"
    short = SESS_SHORT.get(session, session)

    q_candidates = [
        q_dir / f"{year}年{session}_試題.pdf",
        q_dir / f"{year}年{short}_{exam}_試題.pdf",
    ]
    a_candidates = [
        a_dir / f"{year}年{session}_更正答案.pdf",
        a_dir / f"{year}年{session}_答案.pdf",
        a_dir / f"{year}年{short}_{exam}_更正答案.pdf",
        a_dir / f"{year}年{short}_{exam}_標準答案.pdf",
        a_dir / f"{year}年{short}_{exam}_答案.pdf",
    ]
    q_pdf = next((p for p in q_candidates if p.exists()), None)
    a_pdf = next((p for p in a_candidates if p.exists()), None)
    return q_pdf, a_pdf


def scan_contamination(q: dict) -> list[str]:
    flags: list[str] = []
    text = q.get("text") or ""
    choices = q.get("choices") or []

    if HEADER_RE.search(text) or any(HEADER_RE.search(c) for c in choices):
        flags.append("混入頁首頁碼")
    if PUA_RE.search(text) or any(PUA_RE.search(c) for c in choices):
        flags.append("含私用字元")
    if CHOICE_MARK_RE.search(text) or any(CHOICE_MARK_RE.search(c) for c in choices):
        flags.append("殘留舊式選項標記")
    if len(choices) < 4 or len(choices) > 5:
        flags.append(f"選項數異常({len(choices)})")
    if QUESTION_MARK_RE.search(text):
        flags.append("題幹疑似只剩題號")
    return flags


def compare_questions(json_qs: list[dict], src_qs: list[dict]) -> dict:
    json_map = {q["no"]: q for q in json_qs}
    src_map = {q["no"]: q for q in src_qs}
    json_nos = sorted(json_map)
    src_nos = sorted(src_map)

    missing_in_json = [no for no in src_nos if no not in json_map]
    extra_in_json = [no for no in json_nos if no not in src_map]

    text_mismatches = []
    choice_mismatches = []
    contaminated = []

    for no in sorted(set(json_nos) & set(src_nos)):
        jq = json_map[no]
        sq = src_map[no]

        flags = scan_contamination(jq)
        if flags:
            contaminated.append({"no": no, "flags": flags})

        if normalize_text(jq.get("text", "")) != normalize_text(sq.get("text", "")):
            text_mismatches.append(no)

        j_choices = jq.get("choices") or []
        s_choices = sq.get("choices") or []
        if len(j_choices) != len(s_choices):
            choice_mismatches.append(no)
            continue
        for jc, sc in zip(j_choices, s_choices):
            if normalize_text(jc) != normalize_text(sc):
                choice_mismatches.append(no)
                break

    return {
        "json_count": len(json_qs),
        "source_count": len(src_qs),
        "json_nos": json_nos,
        "source_nos": src_nos,
        "missing_in_json": missing_in_json,
        "extra_in_json": extra_in_json,
        "text_mismatches": text_mismatches,
        "choice_mismatches": choice_mismatches,
        "contaminated": contaminated,
    }


def compare_declared_sequence(nos: list[int], declared_count: int | None) -> dict:
    if declared_count is None:
        return {"missing": [], "unexpected": []}
    expected = list(range(1, declared_count + 1))
    expected_set = set(expected)
    actual_set = set(nos)
    return {
        "missing": [no for no in expected if no not in actual_set],
        "unexpected": [no for no in nos if no not in expected_set],
    }


def _normalize_letter_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        value = normalize_text(value).upper()
        if re.fullmatch(r"[A-E]", value) and value not in out:
            out.append(value)
    return out


def _accepted_from_json_question(q: dict) -> list[str]:
    accepted = _normalize_letter_list(q.get("accepted_answers"))
    answer = normalize_text(q.get("answer", "")).upper()
    if not accepted and re.fullmatch(r"[A-E]", answer):
        accepted = [answer]
    if not accepted and q.get("all_correct"):
        count = max(0, min(len(q.get("choices") or []), 5))
        accepted = [chr(ord("A") + i) for i in range(count)]
    return accepted


def _accepted_from_source_meta(meta: dict, choice_count: int) -> list[str]:
    accepted = _normalize_letter_list(meta.get("accepted_answers"))
    raw_answer = normalize_text(meta.get("raw_answer", "")).upper()
    if not accepted and re.fullmatch(r"[A-E]", raw_answer):
        accepted = [raw_answer]
    if not accepted and meta.get("all_correct"):
        count = max(0, min(choice_count, 5))
        accepted = [chr(ord("A") + i) for i in range(count)]
    return accepted


def _is_correction_meta(meta: dict, accepted: list[str]) -> bool:
    raw_answer = normalize_text(meta.get("raw_answer", "")).upper()
    return bool(meta.get("answer_note")) or bool(meta.get("all_correct")) or raw_answer == "#" or len(accepted) > 1


def compare_answers(json_qs: list[dict], src_answer_meta: dict[int, dict]) -> dict:
    json_map = {q["no"]: q for q in json_qs}

    blank_answers: list[int] = []
    blank_without_correction: list[int] = []
    blank_with_correction: list[int] = []
    mismatches: list[dict] = []
    correction_mismatches: list[dict] = []
    unexpected_answers: list[dict] = []

    all_nos = sorted(set(json_map) | set(src_answer_meta))
    for no in all_nos:
        jq = json_map.get(no)
        src_meta = src_answer_meta.get(no, {})
        choice_count = len((jq or {}).get("choices") or [])
        json_accepted = _accepted_from_json_question(jq or {})
        src_accepted = _accepted_from_source_meta(src_meta, choice_count or 4)
        src_is_correction = _is_correction_meta(src_meta, src_accepted)

        if jq and not json_accepted:
            blank_answers.append(no)
            if src_is_correction:
                blank_with_correction.append(no)
            else:
                blank_without_correction.append(no)

        if not jq:
            continue

        if not src_accepted:
            if json_accepted:
                unexpected_answers.append({"no": no, "json": json_accepted, "source": None})
            continue

        if src_is_correction:
            if set(json_accepted) != set(src_accepted) or bool(jq.get("all_correct")) != bool(src_meta.get("all_correct")):
                correction_mismatches.append(
                    {
                        "no": no,
                        "json": json_accepted,
                        "source": src_accepted,
                        "json_all_correct": bool(jq.get("all_correct")),
                        "source_all_correct": bool(src_meta.get("all_correct")),
                    }
                )
            continue

        if set(json_accepted) != set(src_accepted):
            mismatches.append({"no": no, "json": json_accepted, "source": src_accepted})

    return {
        "blank_answers": blank_answers,
        "blank_without_correction": blank_without_correction,
        "blank_with_correction": blank_with_correction,
        "correction_mismatches": correction_mismatches,
        "mismatches": mismatches,
        "unexpected_answers": unexpected_answers,
    }


def verify_one(jf: Path) -> dict:
    data = json.loads(jf.read_text(encoding="utf-8"))
    exam = data["exam"]
    year = data["year"]
    session = data["session"]
    label = f"{exam}_{year}_{session}"
    json_qs = data.get("questions") or []

    q_pdf, a_pdf = find_source_paths(exam, year, session)
    result = {
        "label": label,
        "json_file": str(jf),
        "question_pdf": str(q_pdf) if q_pdf else None,
        "answer_pdf": str(a_pdf) if a_pdf else None,
        "fatal": [],
    }

    if not q_pdf:
        result["fatal"].append("找不到對應題目 PDF")
        return result
    if not a_pdf:
        result["fatal"].append("找不到對應答案 PDF")
        return result

    q_text = extract_text(q_pdf)
    declared_q_count = parse_declared_question_count(q_text)

    src_qs = parse_questions_from_text(q_text)
    choice_counts = {q["no"]: len(q.get("choices") or []) for q in src_qs}
    src_answer_meta = parse_answer_metadata_from_pdf(a_pdf, choice_counts)

    q_cmp = compare_questions(json_qs, src_qs)
    a_cmp = compare_answers(json_qs, src_answer_meta)

    result["question_compare"] = q_cmp
    result["answer_compare"] = a_cmp
    result["declared_question_count"] = declared_q_count
    result["declared_sequence_json"] = compare_declared_sequence(q_cmp["json_nos"], declared_q_count)
    result["declared_sequence_source"] = compare_declared_sequence(q_cmp["source_nos"], declared_q_count)

    if not src_qs:
        result["fatal"].append("原始題目 PDF 重新解析結果為 0 題")
    if not src_answer_meta:
        result["fatal"].append("原始答案 PDF 重新解析結果為 0 題")
    if declared_q_count is not None and q_cmp["json_count"] != declared_q_count:
        result["fatal"].append(f"JSON 題數 {q_cmp['json_count']} 與試卷宣告題數 {declared_q_count} 不符")
    if declared_q_count is not None and q_cmp["source_count"] != declared_q_count:
        result["fatal"].append(f"原始 PDF 重新解析題數 {q_cmp['source_count']} 與試卷宣告題數 {declared_q_count} 不符")
    if result["declared_sequence_json"]["missing"] or result["declared_sequence_json"]["unexpected"]:
        result["fatal"].append(
            f"JSON 題號序列異常：缺 {result['declared_sequence_json']['missing'][:10]} 多 {result['declared_sequence_json']['unexpected'][:10]}"
        )
    if result["declared_sequence_source"]["missing"] or result["declared_sequence_source"]["unexpected"]:
        result["fatal"].append(
            f"原始 PDF 解析題號序列異常：缺 {result['declared_sequence_source']['missing'][:10]} 多 {result['declared_sequence_source']['unexpected'][:10]}"
        )

    severe = (
        q_cmp["json_count"] != q_cmp["source_count"]
        or len(q_cmp["missing_in_json"]) > 0
        or len(q_cmp["extra_in_json"]) > 0
        or len(q_cmp["text_mismatches"]) > 0
        or len(q_cmp["choice_mismatches"]) > 0
        or len(a_cmp["mismatches"]) > 0
        or len(a_cmp["correction_mismatches"]) > 0
        or len(q_cmp["contaminated"]) > 0
        or len(a_cmp["blank_answers"]) > 0
    )
    result["status"] = "NG" if severe or result["fatal"] else "OK"
    return result


def summarize(all_results: list[dict]) -> dict:
    summary = {
        "total_files": len(all_results),
        "ok_files": 0,
        "ng_files": 0,
        "fatal_files": 0,
        "declared_count_mismatch_files": [],
        "declared_sequence_mismatch_files": [],
        "question_count_mismatch_files": [],
        "text_mismatch_files": [],
        "choice_mismatch_files": [],
        "answer_mismatch_files": [],
        "correction_mismatch_files": [],
        "blank_answer_files": [],
        "blank_without_correction_files": [],
        "contaminated_files": [],
        "severely_broken_files": [],
    }

    for r in all_results:
        if r.get("fatal"):
            summary["fatal_files"] += 1
        if r["status"] == "OK":
            summary["ok_files"] += 1
        else:
            summary["ng_files"] += 1

        qc = r.get("question_compare") or {}
        ac = r.get("answer_compare") or {}

        if r.get("declared_question_count") and (
            qc.get("json_count") != r["declared_question_count"]
            or qc.get("source_count") != r["declared_question_count"]
        ):
            summary["declared_count_mismatch_files"].append(r["label"])
        if (r.get("declared_sequence_json", {}).get("missing") or r.get("declared_sequence_json", {}).get("unexpected")
                or r.get("declared_sequence_source", {}).get("missing") or r.get("declared_sequence_source", {}).get("unexpected")):
            summary["declared_sequence_mismatch_files"].append(r["label"])
        if qc.get("json_count") != qc.get("source_count"):
            summary["question_count_mismatch_files"].append(r["label"])
        if qc.get("text_mismatches"):
            summary["text_mismatch_files"].append(r["label"])
        if qc.get("choice_mismatches"):
            summary["choice_mismatch_files"].append(r["label"])
        if ac.get("mismatches"):
            summary["answer_mismatch_files"].append(r["label"])
        if ac.get("correction_mismatches"):
            summary["correction_mismatch_files"].append(r["label"])
        if ac.get("blank_answers"):
            summary["blank_answer_files"].append(r["label"])
        if ac.get("blank_without_correction"):
            summary["blank_without_correction_files"].append(r["label"])
        if qc.get("contaminated"):
            summary["contaminated_files"].append(r["label"])
        declared = r.get("declared_question_count") or 100
        if (
            qc.get("json_count", 0) < declared
            or qc.get("source_count", 0) < declared
            or len(ac.get("blank_without_correction", [])) >= 3
            or len(qc.get("missing_in_json", [])) > 10
        ):
            summary["severely_broken_files"].append(r["label"])

    return summary


def make_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# quiz_json 驗證報告",
        "",
        "來源：原始題目 PDF、原始答案 PDF、`歷屆/quiz_json/*.json`",
        "",
        f"- 總檔數：{summary['total_files']}",
        f"- 完全無異常：{summary['ok_files']}",
        f"- 有問題：{summary['ng_files']}",
        f"- 致命錯誤（缺來源或解析失敗）：{summary['fatal_files']}",
        "",
        "## 主要統計",
        "",
        f"- 與試卷宣告題數不一致：{len(summary['declared_count_mismatch_files'])} 份",
        f"- 題號序列與 1..N 不一致：{len(summary['declared_sequence_mismatch_files'])} 份",
        f"- 題數不一致：{len(summary['question_count_mismatch_files'])} 份",
        f"- 題幹不一致：{len(summary['text_mismatch_files'])} 份",
        f"- 選項不一致：{len(summary['choice_mismatch_files'])} 份",
        f"- 一般答案不一致：{len(summary['answer_mismatch_files'])} 份",
        f"- 更正答案 / 多重給分題表示錯誤：{len(summary['correction_mismatch_files'])} 份",
        f"- 空答案：{len(summary['blank_answer_files'])} 份",
        f"- 空答案且無更正備註可解釋：{len(summary['blank_without_correction_files'])} 份",
        f"- 題目污染（頁碼/私用字元/選項數異常）：{len(summary['contaminated_files'])} 份",
        "",
    ]

    if summary["severely_broken_files"]:
        lines.extend([
            "## 嚴重毀損或高風險檔案",
            "",
        ])
        for label in summary["severely_broken_files"]:
            lines.append(f"- {label}")
        lines.append("")

    lines.extend([
        "## 各檔案摘要",
        "",
    ])

    for r in report["results"]:
        qc = r.get("question_compare") or {}
        ac = r.get("answer_compare") or {}
        lines.append(f"### {r['label']}")
        if r.get("fatal"):
            for item in r["fatal"]:
                lines.append(f"- 致命：{item}")
            lines.append("")
            continue

        lines.append(f"- 狀態：{r['status']}")
        if r.get("declared_question_count"):
            lines.append(
                f"- 題數：JSON {qc['json_count']} / 原始 PDF 解析 {qc['source_count']} / 試卷宣告 {r['declared_question_count']}"
            )
        else:
            lines.append(f"- 題數：JSON {qc['json_count']} / 原始 PDF 解析 {qc['source_count']}")
        if qc["missing_in_json"]:
            lines.append(f"- JSON 缺題號：{qc['missing_in_json'][:20]}")
        if qc["extra_in_json"]:
            lines.append(f"- JSON 多出題號：{qc['extra_in_json'][:20]}")
        if r.get("declared_sequence_json", {}).get("missing"):
            lines.append(f"- JSON 相對 1..N 缺題號：{r['declared_sequence_json']['missing'][:20]}")
        if r.get("declared_sequence_json", {}).get("unexpected"):
            lines.append(f"- JSON 相對 1..N 多出題號：{r['declared_sequence_json']['unexpected'][:20]}")
        if r.get("declared_sequence_source", {}).get("missing"):
            lines.append(f"- 原始 PDF 解析相對 1..N 缺題號：{r['declared_sequence_source']['missing'][:20]}")
        if r.get("declared_sequence_source", {}).get("unexpected"):
            lines.append(f"- 原始 PDF 解析相對 1..N 多出題號：{r['declared_sequence_source']['unexpected'][:20]}")
        if qc["text_mismatches"]:
            lines.append(f"- 題幹不一致題號：{qc['text_mismatches'][:20]}")
        if qc["choice_mismatches"]:
            lines.append(f"- 選項不一致題號：{qc['choice_mismatches'][:20]}")
        if ac["mismatches"]:
            sample = [f"Q{x['no']}: JSON={x['json']!r}, SRC={x['source']!r}" for x in ac["mismatches"][:10]]
            lines.append(f"- 一般答案不一致：{'; '.join(sample)}")
        if ac["correction_mismatches"]:
            sample = [
                f"Q{x['no']}: JSON={x['json']}, SRC={x['source']}, all_correct={x['json_all_correct']}/{x['source_all_correct']}"
                for x in ac["correction_mismatches"][:10]
            ]
            lines.append(f"- 更正答案 / 多重給分表示錯誤：{'; '.join(sample)}")
        if ac["blank_without_correction"]:
            lines.append(f"- 空答案但無更正備註可解釋題號：{ac['blank_without_correction'][:20]}")
        if ac["blank_with_correction"]:
            lines.append(f"- 空答案且屬更正備註題號：{ac['blank_with_correction'][:20]}")
        if qc["contaminated"]:
            sample = [f"Q{x['no']}({','.join(x['flags'])})" for x in qc["contaminated"][:10]]
            lines.append(f"- 內容污染：{'; '.join(sample)}")
        if ac["blank_answers"]:
            lines.append(f"- 空答案題數：{len(ac['blank_answers'])}")
        lines.append("")

    return "\n".join(lines)


def write_report(path: Path, content: str) -> Path:
    try:
        path.write_text(content, encoding="utf-8")
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}.latest{path.suffix}")
        fallback.write_text(content, encoding="utf-8")
        return fallback


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    results = []
    for jf in sorted(JSON_DIR.glob("*.json")):
        result = verify_one(jf)
        results.append(result)
        status = result["status"]
        fatal = f" fatal={len(result.get('fatal', []))}" if result.get("fatal") else ""
        print(f"[{status}] {result['label']}{fatal}")

    report = {
        "summary": summarize(results),
        "results": results,
    }
    json_path = write_report(REPORT_JSON, json.dumps(report, ensure_ascii=False, indent=2))
    md_path = write_report(REPORT_MD, make_markdown(report))

    print()
    print(f"Report JSON: {json_path}")
    print(f"Report MD:   {md_path}")


if __name__ == "__main__":
    main()
