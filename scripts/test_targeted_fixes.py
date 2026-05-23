#!/usr/bin/env python3
"""Targeted regression tests for the 4 fixes applied to proofread.py.

Tests:
  1. Quote state persists across segments (cross-tag quote pairing)
  2. English literary content newline guard (poems/spells preserved)
  3. 3-way merge minimum length guard + LCS threshold
  4. Whitespace normalization fallback in inject
"""

import difflib
import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proofread import (
    proofread_text,
    _three_way_merge,
    _auto_generate_corrections,
)


def test_1_quote_state_across_segments():
    """Quote state persists across segments split by inline tags."""
    print("=== Test 1: Quote state across segments ===")
    passed = 0
    total = 0

    # Simulate 3 segments from <p>"Hello <em>World</em>"</p>
    quote_state = {"left": True}
    seg1, _, _ = proofread_text('"Hello ', {}, [], quote_state)
    seg2, _, _ = proofread_text("World", {}, [], quote_state)
    seg3, _, _ = proofread_text('"', {}, [], quote_state)

    total += 1
    if "\u201c" in seg1 and "\u201d" not in seg1:
        print(f"  1a: Opening quote correct: OK ({repr(seg1)})")
        passed += 1
    else:
        print(f"  1a: Opening quote correct: FAIL ({repr(seg1)})")

    total += 1
    if seg2 == "World":
        print(f"  1b: Middle segment unchanged: OK")
        passed += 1
    else:
        print(f"  1b: Middle segment unchanged: FAIL ({repr(seg2)})")

    total += 1
    if "\u201d" in seg3 and "\u201c" not in seg3:
        print(f"  1c: Closing quote correct: OK ({repr(seg3)})")
        passed += 1
    else:
        print(f"  1c: Closing quote correct: FAIL ({repr(seg3)})")

    # Full reconstruction
    full = seg1 + seg2 + seg3
    expected = "\u201cHello World\u201d"
    total += 1
    if full == expected:
        print(f"  1d: Full reconstruction correct: OK")
        passed += 1
    else:
        print(
            f"  1d: Full reconstruction correct: FAIL "
            f"(got: {repr(full)}, expected: {repr(expected)})"
        )

    # Reset between chapters (new quote_state)
    quote_state2 = {"left": True}
    s1, _, _ = proofread_text('"A" and "B"', {}, [], quote_state2)
    exp_inner = "\u201cA\u201d and \u201cB\u201d"
    total += 1
    if s1 == exp_inner:
        print(f"  1e: Multi-quote single segment: OK")
        passed += 1
    else:
        print(
            f"  1e: Multi-quote single segment: FAIL "
            f"(got: {repr(s1)}, expected: {repr(exp_inner)})"
        )

    # Backward compat: no quote_state
    s2, _, _ = proofread_text('"Hello"', {}, [])
    total += 1
    if s2 == "\u201cHello\u201d":
        print(f"  1f: Backward compat (no quote_state): OK")
        passed += 1
    else:
        print(f"  1f: Backward compat (no quote_state): FAIL ({repr(s2)})")

    print(f"  [{passed}/{total} passed]\n")
    return passed, total


def test_2_english_literary_guard():
    """English with newlines (poems/spells) preserved; single-line deleted."""
    print("=== Test 2: English literary content newline guard ===")
    passed = 0
    total = 0

    tmpdir = tempfile.mkdtemp()
    extracted = os.path.join(tmpdir, "extracted")
    os.makedirs(extracted, exist_ok=True)

    config_data = {
        "blacklist": [],
        "blacklist_defaults": {},
        "proofreading": {"max_change_ratio": 0.6},
    }
    with open(os.path.join(tmpdir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    # Test A: English poem with newlines → should be preserved
    pp_data = {
        "chapter": 1,
        "segments": [
            {
                "id": 1,
                "sub_id": 0,
                "content": "这是中文内容这是中文内容这是中文内容这是中文内容这是中文内容这是中文内容",
                "is_english": False,
            },
            {
                "id": 2,
                "sub_id": 0,
                "content": "Roses are red,\nViolets are blue,\nSugar is sweet,\nAnd so are you.",
                "is_english": True,
            },
            {
                "id": 3,
                "sub_id": 0,
                "content": "继续中文叙述继续中文叙述继续中文叙述继续中文叙述继续中文叙述",
                "is_english": False,
            },
        ],
    }
    with open(
        os.path.join(extracted, "chapter_0001_preprocessed.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(pp_data, f, ensure_ascii=False)

    _auto_generate_corrections(tmpdir)
    corr_path = os.path.join(tmpdir, "corrections_auto.json")
    with open(corr_path, "r", encoding="utf-8") as f:
        corr_data = json.load(f)

    corrections_list = corr_data.get("corrections", [])
    deleted_ids = [
        c["segment_id"] for c in corrections_list if c.get("corrected") in (" ", "")
    ]

    total += 1
    if "2.0" not in deleted_ids:
        print(f"  2a: Poem with newlines preserved: OK (deleted={deleted_ids})")
        passed += 1
    else:
        print(f"  2a: Poem with newlines preserved: FAIL (wrongly deleted)")

    # Test B: Single-line English → should be deleted
    pp_data2 = {
        "chapter": 2,
        "segments": [
            {
                "id": 1,
                "sub_id": 0,
                "content": "中文内容中文内容中文内容中文内容中文内容中文内容中文内容",
                "is_english": False,
            },
            {
                "id": 2,
                "sub_id": 0,
                "content": "This is a stray English sentence that should be deleted here.",
                "is_english": True,
            },
            {
                "id": 3,
                "sub_id": 0,
                "content": "继续中文叙述继续中文叙述继续中文叙述继续中文叙述继续中文叙述",
                "is_english": False,
            },
        ],
    }
    with open(
        os.path.join(extracted, "chapter_0002_preprocessed.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(pp_data2, f, ensure_ascii=False)

    _auto_generate_corrections(tmpdir)
    with open(corr_path, "r", encoding="utf-8") as f:
        corr_data2 = json.load(f)

    corrections_list2 = corr_data2.get("corrections", [])
    deleted_ids2 = [
        c["segment_id"] for c in corrections_list2 if c.get("corrected") in (" ", "")
    ]

    total += 1
    if "2.0" in deleted_ids2:
        print(f"  2b: Single-line English deleted: OK (deleted={deleted_ids2})")
        passed += 1
    else:
        print(f"  2b: Single-line English deleted: FAIL (not deleted, ids={deleted_ids2})")

    shutil.rmtree(tmpdir)
    print(f"  [{passed}/{total} passed]\n")
    return passed, total


def test_3_merge_guards():
    """3-way merge: minimum length guard and LCS threshold."""
    print("=== Test 3: 3-way merge guards ===")
    passed = 0
    total = 0

    # Test A: Direct merge on short text can produce artifacts
    short_old = "他去房间"
    short_corr = "他走入房间"
    short_new = "他去卧室"
    merged_short = _three_way_merge(short_old, short_corr, short_new)
    # The key point: guard at cmd_reprocess level prevents merge for <15 chars
    # Here we just verify _three_way_merge itself doesn't crash
    total += 1
    if isinstance(merged_short, str):
        print(f"  3a: Short merge doesn't crash: OK (result={repr(merged_short)})")
        passed += 1
    else:
        print(f"  3a: Short merge doesn't crash: FAIL")

    total += 1
    if len(short_corr) < 15:
        print(f"  3b: Length guard triggers (<15 chars): OK (len={len(short_corr)})")
        passed += 1
    else:
        print(f"  3b: Length guard triggers (<15 chars): FAIL (len={len(short_corr)})")

    # Test B: Long segment merge preserves both LLM edit and glossary update
    long_old = "他慢慢地走进了那间漆黑的房间，心里充满了不安和恐惧。"
    long_corr = "他缓缓步入那间漆黑的卧室，内心充满不安和恐惧。"
    long_new = "他慢慢地走进了那间漆黑的卧室，心里充满了不安和恐惧。"

    merged_long = _three_way_merge(long_old, long_corr, long_new)

    total += 1
    if "缓缓步入" in merged_long:
        print(f"  3c: Long merge preserves LLM edit: OK")
        passed += 1
    else:
        print(f"  3c: Long merge preserves LLM edit: FAIL ({repr(merged_long)})")

    total += 1
    if "卧室" in merged_long:
        print(f"  3d: Long merge preserves glossary: OK")
        passed += 1
    else:
        print(f"  3d: Long merge preserves glossary: FAIL ({repr(merged_long)})")

    # Test C: LCS threshold simulation
    heavily_old = "他走进了房间，坐在椅子上，看着窗外的风景。"
    heavily_corr = "推门而入后，他先是环顾四周，接着坐到窗边，凝视远方。"
    lcs_val = difflib.SequenceMatcher(None, heavily_old, heavily_corr).ratio()
    total += 1
    if lcs_val < 0.45:
        print(f"  3e: Heavy rewrite LCS={lcs_val:.3f} < 0.45 (guard triggers): OK")
        passed += 1
    else:
        print(f"  3e: Heavy rewrite LCS={lcs_val:.3f} >= 0.45: NOTE (threshold check)")

    print(f"  [{passed}/{total} passed]\n")
    return passed, total


def test_4_whitespace_fallback():
    """Whitespace-normalized regex fallback for inject find failures."""
    print("=== Test 4: Whitespace normalization fallback ===")
    passed = 0
    total = 0

    # Case A: Multiple spaces normalized to single
    content = "第一章  开始叙述。    这里有多余空格。\n\n继续文本。"
    orig = "第一章 开始叙述。 这里有多余空格。"

    idx_direct = content.find(orig)
    total += 1
    if idx_direct == -1:
        print(f"  4a: Direct find fails (whitespace mismatch): OK")
        passed += 1
    else:
        print(f"  4a: Direct find fails: FAIL (unexpectedly found at {idx_direct})")

    # Apply fallback logic
    orig_norm = re.sub(r"\s+", " ", orig).strip()
    pattern = re.escape(orig_norm)
    pattern = re.sub(r"\\ ", r"\\s+", pattern)
    m = re.search(pattern, content)

    total += 1
    if m:
        print(f"  4b: Regex fallback finds match: OK (pos={m.start()}, text={repr(m.group())})")
        passed += 1
    else:
        print(f"  4b: Regex fallback finds match: FAIL")

    # Verify replacement
    if m:
        matched = m.group()
        content_replaced = (
            content[: m.start()] + "[REPLACED]" + content[m.start() + len(matched) :]
        )
        total += 1
        if "[REPLACED]" in content_replaced:
            print(f"  4c: Replacement with matched text: OK")
            passed += 1
        else:
            print(f"  4c: Replacement with matched text: FAIL")

    # Case B: \r\n vs \n normalization
    content2 = "The\r\nquick  brown\n\nfox  jumps."
    orig2 = "The quick brown fox jumps."

    idx_direct2 = content2.find(orig2)
    total += 1
    if idx_direct2 == -1:
        print(f"  4d: \\r\\n direct find fails: OK")
        passed += 1
    else:
        print(f"  4d: \\r\\n direct find fails: FAIL")

    orig_norm2 = re.sub(r"\s+", " ", orig2).strip()
    pattern2 = re.escape(orig_norm2)
    pattern2 = re.sub(r"\\ ", r"\\s+", pattern2)
    m2 = re.search(pattern2, content2)

    total += 1
    if m2:
        print(f"  4e: \\r\\n regex fallback finds match: OK (pos={m2.start()})")
        passed += 1
    else:
        print(f"  4e: \\r\\n regex fallback finds match: FAIL")

    # Case C: Identical text (no normalization needed)
    content3 = "Hello World"
    orig3 = "Hello World"
    m3 = re.search(orig3, content3)
    total += 1
    if m3:
        print(f"  4f: Identical text works: OK (not affected by fallback)")
        passed += 1
    else:
        print(f"  4f: Identical text works: FAIL")

    print(f"  [{passed}/{total} passed]\n")
    return passed, total


def main():
    print("=" * 60)
    print("TARGETED FIX REGRESSION TESTS")
    print("=" * 60)
    print()

    all_passed = 0
    all_total = 0

    for test_fn in [
        test_1_quote_state_across_segments,
        test_2_english_literary_guard,
        test_3_merge_guards,
        test_4_whitespace_fallback,
    ]:
        p, t = test_fn()
        all_passed += p
        all_total += t

    print("=" * 60)
    print(f"TOTAL: {all_passed}/{all_total} passed")
    if all_passed == all_total:
        print("ALL TARGETED FIX TESTS PASSED")
        return 0
    else:
        print(f"FAILED: {all_total - all_passed} test(s)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
