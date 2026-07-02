"""
pdf_to_quiz_json.py
用 PyMuPDF 直接從試題 PDF + 答案 PDF 抽取文字，產生 quiz.html 用的 JSON。
不需要 OCR，速度快、準確度高。

使用方式（單一場次）：
  python pdf_to_quiz_json.py --exam 醫學二 --year 113 --session 第一次
      --q <試題.pdf> --a <答案.pdf>

批次（醫學二 所有年份）：
  python pdf_to_quiz_json.py --batch --exam 醫學二

輸出：D:/一階國考/歷屆/quiz_json/<exam>_<year>_<session>.json
"""
import argparse, json, re, sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("需要 PyMuPDF：pip install pymupdf")


_PHRASE_FIXES = [
    # A few broken PDF glyph mappings collapse multi-character biochemical terms.
    ("去氧", "去氧尿苷酸"),
    ("胸", "胸苷酸"),
    ("核酸", "核苷酸"),
]

_CHAR_FIXES = str.maketrans(
    {
        "\ue2c6": "酶",
        "\ue2c8": "顎",
        "\ue2cc": "疱",
        "\ue2e2": "薦",
        "\ue2e6": "苷",
        "\ueee7": "拇",
        "\uef43": "皰",
    }
)


def clean_extracted_text(text: str) -> str:
    for src, dst in _PHRASE_FIXES:
        text = text.replace(src, dst)
    return text.translate(_CHAR_FIXES)


# ══════════════════════════════════════════════════════════════════════════════
# PDF 文字抽取
# ══════════════════════════════════════════════════════════════════════════════
def extract_text(pdf_path: Path) -> str:
    doc = fitz.open(str(pdf_path))
    pages = []
    for page in doc:
        pages.append(clean_extracted_text(page.get_text()))
    doc.close()
    return "\n".join(pages)


def extract_text_by_column(pdf_path: Path, x_split: float = 0.5) -> tuple[str, str]:
    """
    把每頁依 x 座標切左右兩欄，分別返回（左文字, 右文字）。
    用來解析答案 PDF 的「題號｜答案」雙欄表格。
    """
    doc = fitz.open(str(pdf_path))
    left_parts, right_parts = [], []
    for page in doc:
        w = page.rect.width
        split_x = w * x_split
        left_rect  = fitz.Rect(0,    0, split_x, page.rect.height)
        right_rect = fitz.Rect(split_x, 0, w, page.rect.height)
        left_parts.append(clean_extracted_text(page.get_text(clip=left_rect)))
        right_parts.append(clean_extracted_text(page.get_text(clip=right_rect)))
    doc.close()
    return "\n".join(left_parts), "\n".join(right_parts)


# ══════════════════════════════════════════════════════════════════════════════
# 解析試題 PDF
# ══════════════════════════════════════════════════════════════════════════════
def find_question_sequence(flat: str):
    """
    兩段式題號序列偵測：
    Pass 1 用嚴格模式（題號後不接數字），建立初始序列。
    Pass 2 針對缺口，在前後已知位置之間用寬鬆模式補救
          （可找到「81.50歲」這類題號後直接接數字的情況）。
    """
    # ── Pass 1：嚴格模式 ─────────────────────────────────────────────────────
    pat_strict = re.compile(r"(?<!\d)(\d{1,3})[.．](?!\d)")
    by_no: dict[int, list[int]] = {}
    for m in pat_strict.finditer(flat):
        no = int(m.group(1))
        if 1 <= no <= 300:
            by_no.setdefault(no, []).append(m.start())

    if 1 not in by_no:
        return []

    # ── 建立序列（允許任意缺號，只要 Q1 存在就繼續嘗試） ────────────────────
    def build_seq(by_no: dict) -> list:
        seq: list[tuple[int, int]] = []
        for no in range(1, 301):
            if no not in by_no:
                if seq:          # 已找到至少 1 題 → 容忍缺號
                    continue
                break            # Q1 不在就放棄
            min_pos = seq[-1][0] if seq else -1
            valid = [p for p in by_no[no] if p > min_pos]
            if not valid:
                if seq:
                    continue
                break
            seq.append((min(valid), no))
        return seq

    seq = build_seq(by_no)
    if len(seq) < 5:
        return seq

    # ── Pass 2：對缺口用寬鬆模式在有界區間內補救 ─────────────────────────────
    seq_nos = {no for _, no in seq}
    max_no  = max(seq_nos)

    for target in range(2, max_no + 2):
        if target in seq_nos:
            continue
        # 有界：前一題的位置 < 搜尋範圍 < 後一題的位置
        prev_pos = max((pos for pos, n in seq if n < target), default=0)
        nxt_list = [pos for pos, n in seq if n > target]
        next_pos = min(nxt_list) if nxt_list else len(flat)

        # 完全寬鬆（有界搜尋已足夠安全）：允許題號後接任何字元
        pat2 = re.compile(rf"(?<!\d){target}[.．]")
        cands = [m.start() for m in pat2.finditer(flat, prev_pos, next_pos)
                 if m.start() > prev_pos]
        if cands:
            by_no.setdefault(target, []).extend(cands)
            seq_nos.add(target)

    # ── 重建最終序列 ──────────────────────────────────────────────────────────
    seq = build_seq(by_no)
    return seq


# ══════════════════════════════════════════════════════════════════════════════
# 舊格式解析（104年第一次；題號獨佔一行，選項用私用字元 U+E18C-U+E18F）
# ══════════════════════════════════════════════════════════════════════════════
_PUA_MAP = {'': 0, '': 1, '': 2, '': 3, '': 4}
_PUA_CHARS = set(_PUA_MAP.keys())
_HEADER_LINE_RE = re.compile(r"^(?:代號[:：].*|頁次[:：].*)$")


def parse_pua_questions_legacy(text: str) -> list[dict]:
    """
    解析舊格式 PDF（如 104年第一次）：
    題號是獨佔一行的純數字；選項以私用字元 \\ue18c/d/e/f 開頭（= A/B/C/D）。

    支援兩種子格式：
    - normal：PUA 標記後面直接接選項文字（同行）
    - reversed：選項文字在 PUA 標記的前一行（某些題目的 PDF 排版）
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    questions = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        m = re.fullmatch(r'(\d{1,3})\s*', s)
        if not m:
            i += 1
            continue
        no = int(m.group(1))
        if not (1 <= no <= 300):
            i += 1
            continue

        q_lines: list[str] = []
        choices = ['', '', '', '', '']
        cur = -1         # -1 = 仍在題目；>=0 = 當前選項索引
        fmt = None       # None/'normal'/'reversed'
        pending = None   # reversed 格式：等待 PUA 的選項文字

        i += 1
        while i < len(lines):
            raw = lines[i].rstrip()
            stripped = raw.strip()

            # 遇到純數字行：可能是下一題題號，也可能是選項文字（如「70」分）
            if re.fullmatch(r'\d{1,3}\s*', stripped):
                # 往後看：若緊接著是 PUA 字元，則這是選項文字而非題號
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                next_s = lines[j].strip() if j < len(lines) else ''
                if next_s and next_s[0] in _PUA_CHARS:
                    # 選項文字（reversed 格式）—— 加入 q_lines 待 PUA 行 pop
                    q_lines.append(stripped)
                    i += 1
                    continue
                break  # 下一題題號

            if stripped and stripped[0] in _PUA_CHARS:
                cur = _PUA_MAP[stripped[0]]
                text_after = stripped[1:].strip()
                if text_after:
                    # normal 格式：PUA 標記 + 同行文字
                    if fmt is None:
                        fmt = 'normal'
                    choices[cur] = text_after
                    pending = None
                else:
                    # lone PUA：reversed 格式或第一個 normal 選項無文字
                    if fmt is None:
                        fmt = 'reversed'
                    # 取 pending（前一個文字行）或 q_lines 最後一行
                    if pending is not None:
                        choices[cur] = pending
                        pending = None
                    elif q_lines:
                        choices[cur] = q_lines.pop()
            elif stripped == '':
                # 空行：reversed 格式重置 pending（不累加到錯誤選項）
                if fmt == 'reversed':
                    pending = None
            else:
                # 非空、非 PUA 文字行
                if fmt == 'reversed' and cur >= 0:
                    # reversed 格式中，choice 區段的文字行是下一選項的文字
                    pending = stripped
                elif fmt == 'normal' and cur >= 0:
                    # normal 格式中，是當前選項的續行
                    choices[cur] += ' ' + stripped
                else:
                    # 題目文字
                    q_lines.append(stripped)

            i += 1

        q_text = ' '.join(q_lines)
        choice_list = [choices[k] for k in range(4) if choices[k]]
        if q_text and len(choice_list) >= 2:
            questions.append({'no': no, 'text': q_text,
                              'choices': choice_list, 'answer': ''})

    return questions


def parse_flat_questions_legacy(text: str) -> list[dict]:
    flat = " ".join(text.split())
    seq = find_question_sequence(flat)
    if len(seq) < 5:
        return []

    # 選項模式（A. / (A) / A） ＋必須前有空格
    choice_re = re.compile(
        r"(?<=\s)([A-Ea-e])[.．）\)]\s*(.+?)(?=\s[A-Ea-e][.．）\)]|\s\d{1,3}[.．](?!\d)|$)"
    )

    questions = []
    for idx, (pos, no) in enumerate(seq):
        end = seq[idx + 1][0] if idx + 1 < len(seq) else len(flat)
        seg = flat[pos:end]
        seg = re.sub(r"^\s*\d{1,3}[.．]\s*", "", seg).strip()

        matches = list(choice_re.finditer(" " + seg))
        if matches:
            # matches[0].start() 是在 " "+seg 中的位置（比 seg 多偏移 1）
            q_text = seg[: matches[0].start() - 1].strip()
            choices = [m.group(2).strip() for m in matches][:5]
        else:
            q_text = seg
            choices = []

        choices = [re.sub(r"\s+", " ", c) for c in choices]
        if q_text:
            questions.append({"no": no, "text": q_text, "choices": choices, "answer": ""})

    return questions


def normalize_inline_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_declared_question_count(text: str) -> int | None:
    flat = normalize_inline_text(text)
    patterns = [
        r"本科目共\s*(\d{1,3})\s*題",
        r"單選題數[:：]?\s*(\d{1,3})\s*題",
        r"題\s*數[:：]?\s*(\d{1,3})\s*題",
    ]
    for pat in patterns:
        m = re.search(pat, flat)
        if m:
            return int(m.group(1))
    return None


def match_question_start(line: str, expected: int) -> str | None:
    s = line.strip()
    if not s or _HEADER_LINE_RE.match(s):
        return None
    m = re.match(rf"^{expected}(?:\s*$|[.．、\)）]\s*|\s+)(.*)$", s)
    if not m:
        return None
    return m.group(1).strip()


def is_chart_or_table_line(line: str) -> bool:
    s = normalize_inline_text(line)
    if not s:
        return False
    if s in {"時間", "Total", "有", "無", "總人數", "疾病真實狀況", "疾病診斷結果"}:
        return True
    if re.fullmatch(r"[\d\s.%xX×\-+/:]+", s):
        return True
    if re.fullmatch(r"(?:\d+(?:\.\d+)?\s*){2,}", s):
        return True
    if re.fullmatch(r"[A-Za-z]\d{1,3}", s):
        return True
    return False


def append_text(base: str, extra: str) -> str:
    extra = normalize_inline_text(extra)
    if not extra:
        return base
    return extra if not base else f"{base} {extra}"


def iter_choice_matches(line: str, use_pua: bool):
    if use_pua:
        pat = re.compile(r"([])")
        for m in pat.finditer(line):
            yield m, _PUA_MAP[m.group(1)]
        return

    pat = re.compile(r"(?:(?<=^)|(?<=\s))([A-E])[.．、\)）]\s*")
    for m in pat.finditer(line):
        yield m, ord(m.group(1)) - ord("A")


def parse_choice_block_linewise(no: int, lines: list[str], use_pua: bool) -> dict | None:
    q_parts: list[str] = []
    choices = ["", "", "", "", ""]
    current_idx: int | None = None
    seen_choice = False
    max_idx = -1

    for raw in lines:
        line = raw.strip()
        if not line or _HEADER_LINE_RE.match(line):
            continue

        matches = list(iter_choice_matches(line, use_pua))
        if not matches:
            if not seen_choice:
                q_parts.append(line)
            elif current_idx is not None:
                if is_chart_or_table_line(line):
                    q_parts.append(line)
                else:
                    choices[current_idx] = append_text(choices[current_idx], line)
            else:
                q_parts.append(line)
            continue

        first_match = matches[0][0]
        prefix = line[: first_match.start()].strip()
        if prefix:
            if not seen_choice or is_chart_or_table_line(prefix):
                q_parts.append(prefix)
            elif current_idx is not None:
                choices[current_idx] = append_text(choices[current_idx], prefix)

        for idx_match, (m, choice_idx) in enumerate(matches):
            max_idx = max(max_idx, choice_idx)
            next_start = matches[idx_match + 1][0].start() if idx_match + 1 < len(matches) else len(line)
            content = line[m.end() : next_start].strip()
            if not content and not seen_choice and q_parts:
                last = q_parts[-1]
                if not last.endswith(("？", "?", "：", ":")) and not is_chart_or_table_line(last):
                    content = q_parts.pop()

            seen_choice = True
            current_idx = choice_idx
            if content:
                choices[choice_idx] = append_text(choices[choice_idx], content)

    q_text = normalize_inline_text(" ".join(q_parts))
    if not q_text:
        return None

    choice_list: list[str] = []
    if max_idx >= 0:
        for idx in range(max_idx + 1):
            cleaned = normalize_inline_text(choices[idx])
            if not cleaned:
                cleaned = f"[選項 {chr(ord('A') + idx)} 文字未擷取]"
            choice_list.append(cleaned)

    return {"no": no, "text": q_text, "choices": choice_list, "answer": ""}


def parse_questions_linewise(text: str) -> list[dict]:
    lines = [ln.rstrip() for ln in text.splitlines()]
    questions = []
    expected = 1
    i = 0

    while i < len(lines) and expected <= 300:
        rest = None
        while i < len(lines):
            rest = match_question_start(lines[i], expected)
            if rest is not None:
                break
            i += 1
        if rest is None:
            break

        segment: list[str] = []
        if rest:
            segment.append(rest)
        i += 1

        while i < len(lines):
            next_rest = match_question_start(lines[i], expected + 1)
            if next_rest is not None:
                break
            segment.append(lines[i])
            i += 1

        joined = "\n".join(segment)
        use_pua = any(ch in joined for ch in _PUA_CHARS)
        q = parse_choice_block_linewise(expected, segment, use_pua)
        if q:
            questions.append(q)
        expected += 1

    return questions


def score_question_parse(questions: list[dict], text: str) -> int:
    if not questions:
        return -10**9

    declared = parse_declared_question_count(text) or 100
    nos = [q["no"] for q in questions]
    no_set = set(nos)
    expected_set = set(range(1, declared + 1))

    score = len(questions) * 50
    score -= abs(len(questions) - declared) * 300
    score -= len(expected_set - no_set) * 500
    score -= len([n for n in nos if n not in expected_set]) * 500

    for q in questions:
        q_text = q.get("text") or ""
        choices = q.get("choices") or []
        if len(choices) < 4:
            score -= 80
        if any("未擷取" in c for c in choices):
            score -= 10 * sum("未擷取" in c for c in choices)
        if "代號" in q_text or "頁次" in q_text:
            score -= 25
        score -= 5 * sum(("代號" in c or "頁次" in c) for c in choices)
        score -= 2 * sum(any(ch in c for ch in _PUA_CHARS) for c in choices)
        if any(ch in q_text for ch in _PUA_CHARS):
            score -= 10

    return score


def parse_questions_from_text(text: str) -> list[dict]:
    candidates: list[list[dict]] = []

    linewise = parse_questions_linewise(text)
    if linewise:
        candidates.append(linewise)

    if any(ch in text for ch in _PUA_CHARS):
        legacy_pua = parse_pua_questions_legacy(text)
        if legacy_pua:
            candidates.append(legacy_pua)

    legacy_flat = parse_flat_questions_legacy(text)
    if legacy_flat:
        candidates.append(legacy_flat)

    if not candidates:
        return []

    return max(candidates, key=lambda qs: score_question_parse(qs, text))


# ══════════════════════════════════════════════════════════════════════════════
# 解析答案 PDF
# ══════════════════════════════════════════════════════════════════════════════
def fullwidth_to_ascii(s: str) -> str:
    """把全形字母 ＡＢＣＤＥ → ABCDE"""
    return s.translate(str.maketrans("ＡＢＣＤＥ＃", "ABCDE#"))


def extract_visual_lines(pdf_path: Path) -> list[list[str]]:
    doc = fitz.open(str(pdf_path))
    lines: list[list[str]] = []
    for page in doc:
        words = page.get_text("words", sort=True)
        current: list[str] = []
        current_y: float | None = None
        for x0, y0, x1, y1, word, block_no, line_no, word_no in words:
            text = fullwidth_to_ascii(word).strip()
            if not text:
                continue
            if current_y is None or abs(y0 - current_y) <= 1.5:
                current.append(text)
                if current_y is None:
                    current_y = y0
            else:
                lines.append(current)
                current = [text]
                current_y = y0
        if current:
            lines.append(current)
    doc.close()
    return lines


def expand_answer_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for tok in tokens:
        s = fullwidth_to_ascii(tok).replace("　", "")
        if re.fullmatch(r"答案[A-E#]", s):
            out.extend(["答案", s[-1]])
        else:
            out.append(s)
    return out


def extract_question_numbers_from_tokens(tokens: list[str]) -> list[int]:
    nums: list[int] = []
    for tok in tokens:
        m = re.fullmatch(r"第(\d{1,3})題", tok)
        if m:
            nums.append(int(m.group(1)))
            continue
        m = re.fullmatch(r"0?(\d{1,3})", tok)
        if m:
            nums.append(int(m.group(1)))
    return [n for n in nums if 1 <= n <= 300]


def extract_answer_letters_from_tokens(tokens: list[str]) -> list[str]:
    answers: list[str] = []
    for tok in tokens:
        if tok == "答案":
            continue
        if re.fullmatch(r"[A-E#]", tok):
            answers.append(tok)
    return answers


def parse_raw_answers_from_visual_layout(pdf_path: Path) -> dict[int, str]:
    lines = extract_visual_lines(pdf_path)
    answers: dict[int, str] = {}
    pending_numbers: list[int] | None = None
    pending_answers: list[str] = []

    for raw_tokens in lines:
        tokens = expand_answer_tokens(raw_tokens)
        if not tokens:
            continue

        has_num_header = any(tok in ("題號", "题号", "題序", "题序") for tok in tokens)
        nums = extract_question_numbers_from_tokens(tokens)
        ans = extract_answer_letters_from_tokens(tokens)

        if has_num_header and nums:
            pending_numbers = nums
            pending_answers = []
        elif nums and not ans and len(nums) >= 5:
            pending_numbers = nums
            pending_answers = []

        if ans and pending_numbers:
            pending_answers.extend(ans)

        if pending_numbers and len(pending_answers) >= len(pending_numbers):
            for no, letter in zip(pending_numbers, pending_answers):
                answers[no] = letter
            pending_numbers = None
            pending_answers = []

    return answers


def parse_raw_answers_from_text_legacy(pdf_path: Path) -> dict[int, str]:
    text = fullwidth_to_ascii(extract_text(pdf_path))
    answers: dict[int, str] = {}

    lines = [l.strip() for l in text.splitlines()]
    groups: list[list[str]] = []
    i = 0
    while i < len(lines):
        if lines[i] in ("題號", "题号", "題序", "题序"):
            i += 1
            group: list[str] = []
            while i < len(lines) and len(group) < 20:
                ln = lines[i].strip()
                if ln.startswith("答案"):
                    letter_part = ln[2:].strip()
                    for ch in letter_part:
                        if re.fullmatch(r"[A-E#]", ch):
                            group.append(ch)
                            break
                elif re.fullmatch(r"[A-E#]", ln):
                    group.append(ln)
                i += 1
            if group:
                groups.append(group)
        else:
            i += 1

    if groups:
        q_no = 1
        for grp in groups:
            for letter in grp:
                answers[q_no] = letter
                q_no += 1
        if len(answers) >= 50:
            return answers

    flat = " ".join(text.split())
    pats = [
        r"(\d{1,3})[.．\s]*[（\(]([A-E#])[）\)]",
        r"(\d{1,3})[.．]\s*([A-E#])(?![a-zA-Z\d])",
        r"(?<!\d)(\d{1,3})\s+([A-E#])(?!\w)",
    ]
    for pat in pats:
        for m in re.finditer(pat, flat):
            no = int(m.group(1))
            if 1 <= no <= 300:
                answers.setdefault(no, m.group(2))
        if len(answers) >= 50:
            break

    return answers


def parse_correction_notes_from_text(text: str, choice_counts: dict[int, int] | None = None) -> dict[int, dict]:
    flat = normalize_inline_text(fullwidth_to_ascii(text))
    note_start = re.search(r"備\s*註[:：]", flat)
    if note_start:
        flat = flat[note_start.end() :]
    note_end = flat.find("標準答案")
    if note_end > 0:
        flat = flat[:note_end]
    notes: dict[int, dict] = {}

    for m in re.finditer(r"第\s*(\d{1,3})題(.*?)(?=第\s*\d{1,3}題|$)", flat):
        no = int(m.group(1))
        clause = normalize_inline_text(m.group(2))
        clause_compact = re.sub(r"\s+", "", clause)
        if "給分" not in clause_compact and "更正" not in clause_compact:
            continue

        accepted: list[str] = []
        all_correct = False
        if "一律給分" in clause_compact or re.search(r"其餘(?:均)?給分", clause_compact):
            count = choice_counts.get(no, 4) if choice_counts else 4
            accepted = [chr(ord("A") + i) for i in range(max(0, min(count, 5)))]
            all_correct = True
        else:
            before_score = clause_compact.split("給分", 1)[0]
            for ch in re.findall(r"[A-E]", before_score):
                if ch not in accepted:
                    accepted.append(ch)

        notes[no] = {
            "accepted_answers": accepted,
            "answer_note": clause,
        }
        if all_correct:
            notes[no]["all_correct"] = True

    return notes


def parse_answer_metadata_from_pdf(pdf_path: Path, choice_counts: dict[int, int] | None = None) -> dict[int, dict]:
    raw_answers = parse_raw_answers_from_visual_layout(pdf_path)
    if len(raw_answers) < 50:
        raw_answers = parse_raw_answers_from_text_legacy(pdf_path)

    correction_notes = parse_correction_notes_from_text(extract_text(pdf_path), choice_counts)
    meta: dict[int, dict] = {}

    for no, raw in raw_answers.items():
        item = {"raw_answer": raw}
        if raw and raw != "#":
            item["accepted_answers"] = [raw]
        meta[no] = item

    for no, note in correction_notes.items():
        item = meta.setdefault(no, {})
        item.update(note)

    return meta


def parse_answers_from_pdf(pdf_path: Path) -> dict[int, str]:
    raw_answers = parse_raw_answers_from_visual_layout(pdf_path)
    if len(raw_answers) < 50:
        raw_answers = parse_raw_answers_from_text_legacy(pdf_path)
    return {no: ("" if ans == "#" else ans) for no, ans in raw_answers.items()}


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════
def process_one(q_pdf: Path, a_pdf: Path, exam: str, year: str, session: str, out_path: Path) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"\n  -- {exam} {year}年{session} --")

    if not q_pdf.exists():
        print(f"  [skip] 試題不存在：{q_pdf}")
        return 0

    text = extract_text(q_pdf)
    questions = parse_questions_from_text(text)
    choice_counts = {q["no"]: len(q.get("choices") or []) for q in questions}
    answer_meta = parse_answer_metadata_from_pdf(a_pdf, choice_counts) if a_pdf.exists() else {}

    no_ans = []
    for q in questions:
        meta = answer_meta.get(q["no"], {})
        raw_answer = meta.get("raw_answer", "")
        accepted = list(meta.get("accepted_answers") or [])

        if accepted:
            q["answer"] = accepted[0]
            if len(accepted) > 1 or meta.get("all_correct"):
                q["accepted_answers"] = accepted
            if meta.get("all_correct"):
                q["all_correct"] = True
        else:
            q["answer"] = raw_answer if raw_answer and raw_answer != "#" else ""

        if meta.get("answer_note"):
            q["answer_note"] = meta["answer_note"]
        if raw_answer and raw_answer != q["answer"]:
            q["answer_source"] = raw_answer

        if not q["answer"] and not q.get("accepted_answers"):
            no_ans.append(q["no"])

    data = {"exam": exam, "year": year, "session": session, "questions": questions}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    warn = f" | missing ans: {no_ans[:5]}" if no_ans else ""
    print(f"  -> {len(questions)} Q, {len(answer_meta)} ans/meta{warn}")
    print(f"     {out_path}")
    return len(questions)


SESSION_NAME = {"第一次": "第一次", "第二次": "第二次"}

def run_batch(exam: str, pdf_q_dir: Path, pdf_a_dir: Path, out_dir: Path):
    """
    掃描試題資料夾（如 D:/一階國考/歷屆/醫學二/題目/*.pdf）
    並自動找對應答案 PDF。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    n = 0

    for q_pdf in sorted(pdf_q_dir.glob("*.pdf")):
        name = q_pdf.stem  # e.g. "113年第一次_試題"
        m = re.match(r"(\d+)年?(第[一二]次)?", name)
        if not m:
            continue
        year = m.group(1)
        session = m.group(2) or "第一次"

        # 找對應答案 PDF（優先更正答案）
        # 醫學一 答案 PDF 用「第1次」阿拉伯數字且含科目名稱
        num_map = {"第一次": "第1次", "第二次": "第2次"}
        short = num_map.get(session, session)
        a_candidates = [
            # 醫學二格式
            pdf_a_dir / f"{year}年{session}_更正答案.pdf",
            pdf_a_dir / f"{year}年{session}_答案.pdf",
            # 醫學一格式（含科目名 + 阿拉伯場次）
            pdf_a_dir / f"{year}年{short}_{exam}_更正答案.pdf",
            pdf_a_dir / f"{year}年{short}_{exam}_標準答案.pdf",
            pdf_a_dir / f"{year}年{short}_{exam}_答案.pdf",
            # 通用備援
            pdf_a_dir / f"{name.replace('_試題','_更正答案')}.pdf",
            pdf_a_dir / f"{name.replace('_試題','_答案')}.pdf",
        ]
        a_pdf = next((p for p in a_candidates if p.exists()), Path("__missing__.pdf"))

        out_path = out_dir / f"{exam}_{year}_{session}.json"
        total += process_one(q_pdf, a_pdf, exam, year, session, out_path)
        n += 1

    print(f"\n[Done] {n} files, {total} questions -> {out_dir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exam",    default="醫學二")
    ap.add_argument("--q",       help="試題 PDF（單一）")
    ap.add_argument("--a",       help="答案 PDF（單一）")
    ap.add_argument("--year",    help="年份（單一）")
    ap.add_argument("--session", default="第一次", help="場次（單一）")
    ap.add_argument("--out",     help="JSON 輸出路徑（單一）")
    ap.add_argument("--batch",   action="store_true")
    ap.add_argument("--q-dir",   default=r"D:\一階國考\歷屆\醫學二\題目")
    ap.add_argument("--a-dir",   default=r"D:\一階國考\歷屆\醫學二\答案")
    ap.add_argument("--json-out",default=r"D:\一階國考\歷屆\quiz_json")
    args = ap.parse_args()

    if args.batch:
        run_batch(
            args.exam,
            Path(args.q_dir),
            Path(args.a_dir),
            Path(args.json_out),
        )
    else:
        if not (args.q and args.year):
            ap.error("需要 --q 和 --year")
        q_pdf   = Path(args.q)
        a_pdf   = Path(args.a) if args.a else Path("__missing__.pdf")
        out_path = Path(args.out) if args.out else \
                   Path(args.json_out) / f"{args.exam}_{args.year}_{args.session}.json"
        process_one(q_pdf, a_pdf, args.exam, args.year, args.session, out_path)


if __name__ == "__main__":
    main()
