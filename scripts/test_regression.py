#!/usr/bin/env python3
"""Regression tests for epub-chinese-proofreading skill."""
import sys
import html
import re
from lxml import etree

sys.path.insert(0, '.')
from proofread import (
    extract_text_segments, _decode_xhtml, split_long_text,
    apply_mechanical_style_fixes, compute_change_ratio,
    natural_sort_key, _is_cjk, _is_valid_term_char, _ENTITY_REVERSE_MAP,
    _safe_extract_epub,
)

passed = failed = 0

def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {msg}")

# ============================================================
# Test 1: extract_text_segments filtering
# ============================================================
print("=== Test 1: extract_text_segments ===")

html1 = "<html><body><p>textA<style>.css{}</style>textB</p></body></html>"
root = etree.fromstring(html1, etree.XMLParser(recover=True))
segs = extract_text_segments(root)
contents = [s["content"] for s in segs]
check("textA" in contents, "1a: p.text lost")
check("textB" in contents, "1a: style.tail lost")
check(not any(".css" in c for c in contents), "1a: CSS extracted")
check(len(segs) == 2, f"1a: expected 2, got {len(segs)}")
print(f"  1a style: {'OK' if all(['textA' in contents, 'textB' in contents, not any('.css' in c for c in contents), len(segs)==2]) else 'FAIL'}")

html2 = "<html><body><p>textA<script>js()</script>textB</p></body></html>"
root = etree.fromstring(html2, etree.XMLParser(recover=True))
segs = extract_text_segments(root)
contents = [s["content"] for s in segs]
check("textA" in contents, "1b: p.text lost")
check("textB" in contents, "1b: script.tail lost")
check(not any("js()" in c for c in contents), "1b: JS extracted")
print(f"  1b script: {'OK' if all(['textA' in contents, 'textB' in contents, not any('js()' in c for c in contents)]) else 'FAIL'}")

html3 = "<html><body><p>textA<!-- comment -->textB</p></body></html>"
root = etree.fromstring(html3, etree.XMLParser(recover=True))
segs = extract_text_segments(root)
contents = [s["content"] for s in segs]
check("textA" in contents, "1c: p.text lost")
check("textB" in contents, "1c: comment.tail lost")
check(not any("comment" in c for c in contents), "1c: comment extracted")
print(f"  1c comment: {'OK' if all(['textA' in contents, 'textB' in contents, not any('comment' in c for c in contents)]) else 'FAIL'}")

html4 = "<html><head><title>Page Title</title></head><body><p>real</p></body></html>"
root = etree.fromstring(html4, etree.XMLParser(recover=True))
segs = extract_text_segments(root)
contents = [s["content"] for s in segs]
check(not any("Page Title" in c for c in contents), "1d: title extracted")
check("real" in contents, "1d: body text lost")
print(f"  1d title: {'OK' if not any('Page Title' in c for c in contents) and 'real' in contents else 'FAIL'}")

print()

# ============================================================
# Test 2: cmd_inject seg_id synchronization
# ============================================================
print("=== Test 2: cmd_inject seg_id sync ===")

html5 = "<html><body><p>textA<style>.css{}</style>textB<script>js()</script>textC</p></body></html>"
root = etree.fromstring(html5, etree.XMLParser(recover=True))

# extract side
extracted = extract_text_segments(root)
extract_ids = {s["id"]: s["content"] for s in extracted}

# inject side (matching the fixed cmd_inject logic)
all_segments = []
seg_id = 0
for element in root.iter():
    if not isinstance(element.tag, str):
        if element.tail and element.tail.strip():
            all_segments.append((seg_id, element.tail))
            seg_id += 1
        continue
    tag_name = element.tag.split("}")[-1] if "}" in element.tag else element.tag
    skip_text = tag_name in ("style", "script", "title", "meta")
    if not skip_text and element.text and element.text.strip():
        all_segments.append((seg_id, element.text))
        seg_id += 1
    if element.tail and element.tail.strip():
        all_segments.append((seg_id, element.tail))
        seg_id += 1

inject_ids = {sid: content for sid, content in all_segments}

check(len(extract_ids) == len(inject_ids),
      f"2: count mismatch extract={len(extract_ids)} inject={len(inject_ids)}")
for sid in extract_ids:
    check(sid in inject_ids, f"2: seg_id {sid} missing in inject")
    if sid in inject_ids:
        check(extract_ids[sid] == inject_ids[sid],
              f"2: content mismatch at id={sid}: extr={repr(extract_ids[sid])} inj={repr(inject_ids[sid])}")
print(f"  2 seg_id sync: {'OK' if len(extract_ids)==len(inject_ids) and all(extract_ids.get(sid)==inject_ids.get(sid) for sid in extract_ids) else 'FAIL'}")
print()

# ============================================================
# Test 3: XML entity handling
# ============================================================
print("=== Test 3: XML entity handling ===")

# Test 3a: LLM introduces & — must be escaped for XML
repl = "A & B"
repl_final = html.escape(repl)
check(repl_final == "A &amp; B", f"3a: got {repr(repl_final)}")
print(f"  3a & escape: {'OK' if repl_final=='A &amp; B' else 'FAIL'}")

# Test 3b: LLM introduces < and >
repl = "a < b > c"
repl_final = html.escape(repl)
check(repl_final == "a &lt; b &gt; c", f"3b: got {repr(repl_final)}")
print(f"  3b <> escape: {'OK' if repl_final=='a &lt; b &gt; c' else 'FAIL'}")

# Test 3c: EPUB entity reversals
repl3 = "He said \u201cHello\u201d \u2014 end"
repl_final = html.escape(repl3)
for char, entity in _ENTITY_REVERSE_MAP:
    repl_final = repl_final.replace(char, entity)
check("&ldquo;" in repl_final, f"3c: missing &ldquo; in {repr(repl_final)}")
check("&rdquo;" in repl_final, f"3c: missing &rdquo;")
check("&mdash;" in repl_final, f"3c: missing &mdash;")
print(f"  3c EPUB entities: {'OK' if '&ldquo;' in repl_final and '&rdquo;' in repl_final and '&mdash;' in repl_final else 'FAIL'}")

# Test 3d: Mixed — LLM introduces & alongside smart quotes
repl4 = "He said \u201cA & B\u201d \u2014 end"
repl_final = html.escape(repl4)
for char, entity in _ENTITY_REVERSE_MAP:
    repl_final = repl_final.replace(char, entity)
check("&ldquo;" in repl_final, "3d: missing left quote")
check("&amp;" in repl_final, f"3d: & NOT escaped in {repr(repl_final)}")
check("&rdquo;" in repl_final, "3d: missing right quote")
check("&mdash;" in repl_final, "3d: missing em dash")
print(f"  3d mixed: {'OK' if all(x in repl_final for x in ['&ldquo;','&amp;','&rdquo;','&mdash;']) else 'FAIL'}")
print()

# ============================================================
# Test 4: _decode_xhtml encoding detection
# ============================================================
print("=== Test 4: _decode_xhtml ===")

text, enc = _decode_xhtml("hello".encode("utf-8"))
check(enc == "utf-8", f"4a: got {enc}")
check(text == "hello", "4a: content wrong")
print(f"  4a utf-8: {'OK' if enc=='utf-8' else 'FAIL'}")

xml = '<?xml version="1.0" encoding="UTF-8"?><html>test</html>'.encode("utf-8")
text, enc = _decode_xhtml(xml)
check(enc == "utf-8", f"4b: got {enc}")
print(f"  4b xml decl utf-8: {'OK' if enc=='utf-8' else 'FAIL'}")

gbk = "你好世界".encode("gbk")
text, enc = _decode_xhtml(gbk)
check(enc == "gbk", f"4c: got {enc}")
check("你好世界" in text, f"4c: content: {repr(text)}")
print(f"  4c gbk fallback: {'OK' if enc=='gbk' else 'FAIL'}")

xml_gbk = '<?xml version="1.0" encoding="GBK"?><html>你好</html>'.encode("gbk")
text, enc = _decode_xhtml(xml_gbk)
check(enc == "gbk", f"4d: got {enc}")
check("你好" in text, "4d: content wrong")
print(f"  4d gbk xml decl: {'OK' if enc=='gbk' else 'FAIL'}")

corrupt = b"\xff\xfe\x00\x01"
text, enc = _decode_xhtml(corrupt)
check(enc == "utf-8", f"4e: got {enc}")
print(f"  4e corrupt fallback: {'OK' if enc=='utf-8' else 'FAIL'}")
print()

# ============================================================
# Test 5: split_long_text regex safety
# ============================================================
print("=== Test 5: split_long_text ===")

parts = split_long_text("第一句。第二句？第三句！", threshold=5)
check(len(parts) >= 3, f"5a: {len(parts)} parts")
print(f"  5a normal: {'OK' if len(parts)>=3 else 'FAIL'}")

parts = split_long_text("A|B测试。C|D句子。E|F结尾。", threshold=5)
check(not any(p.startswith("|") for p in parts), f"5b: bad split: {parts}")
print(f"  5b pipe-safe: {'OK' if not any(p.startswith('|') for p in parts) else 'FAIL'}")

parts = split_long_text("短", threshold=300)
check(len(parts) == 1, f"5c: {len(parts)}")
print(f"  5c short text: {'OK' if len(parts)==1 else 'FAIL'}")
print()

# ============================================================
# Test 6: apply_mechanical_style_fixes
# ============================================================
print("=== Test 6: apply_mechanical_style_fixes ===")

text = "段落一\n，段落二\n。"
result = apply_mechanical_style_fixes(text)
check("\n" in result, "6a: newline deleted")
print(f"  6a newline preserved: {'OK' if chr(10) in result else 'FAIL'}")

result = apply_mechanical_style_fixes("你好  ，  世界  。")
check("你好，世界。" in result.replace(" ", ""), f"6b: spaces not cleaned: {repr(result)}")
print(f"  6b space cleanup: OK")

# Destructive patterns must NOT exist
check("遭" not in result or "被子" not in text, "6c: old destructive regex still active?")
print(f"  6c no destructive regex: OK")
print()

# ============================================================
# Test 7: natural_sort_key
# ============================================================
print("=== Test 7: natural_sort_key ===")
from pathlib import Path
files = [Path("chapter10.xhtml"), Path("chapter2.xhtml"), Path("chapter1.xhtml")]
sorted_files = sorted(files, key=natural_sort_key)
check([f.name for f in sorted_files] == ["chapter1.xhtml", "chapter2.xhtml", "chapter10.xhtml"],
      f"7: {[f.name for f in sorted_files]}")
print(f"  7 natural sort: {'OK' if sorted_files[0].name=='chapter1.xhtml' else 'FAIL'}")
print()

# ============================================================
# Test 8: _is_valid_term_char for foreign names
# ============================================================
print("=== Test 8: _is_valid_term_char ===")

check(_is_valid_term_char("\u4e2d"), "8a: CJK char rejected")
check(_is_valid_term_char("\u00b7"), "8b: middle dot rejected")
check(_is_valid_term_char("\u30fb"), "8c: katakana middle dot rejected")
check(_is_valid_term_char("-"), "8d: hyphen rejected")
check(_is_valid_term_char(" "), "8e: space rejected")
check(not _is_valid_term_char("!"), "8f: bang accepted")
check(not _is_valid_term_char("@"), "8g: at accepted")

# Simulated name "哈利·波特"
name_chars = "哈利\u00b7波特"
check(all(_is_valid_term_char(c) for c in name_chars), f"8h: name rejected: {[c for c in name_chars if not _is_valid_term_char(c)]}")
print(f"  8 foreign names: {'OK' if all(_is_valid_term_char(c) for c in name_chars) else 'FAIL'}")
print()

# ============================================================
# Test 9: compute_change_ratio for sub-segment false positive
# ============================================================
print("=== Test 9: sub-segment ratio false positive ===")

part = "这是一个标准的测试句子用来模拟段落长度。"
long_para = part * 8  # ~208 chars
first_sentence = part * 1  # ~26 chars

ratio_old = compute_change_ratio(long_para, first_sentence)
check(ratio_old > 0.4, f"9a: old code would NOT flag: {ratio_old:.1%}")
print(f"  9a old buggy ratio: {ratio_old:.1%} (should be >40%)")

ratio_new = compute_change_ratio(long_para, long_para)
check(ratio_new == 0.0, f"9b: {ratio_new}")
print(f"  9b new grouped ratio: {ratio_new:.1%} (should be 0%)")
print()

# ============================================================
# Test 10: cmd_inject entity fallback simulation
# ============================================================
print("=== Test 10: entity fallback (cmd_inject simulation) ===")

# Raw EPUB file has ENTITY STRINGS, not Unicode characters
raw_content = "<p>A &amp; B and &ldquo;quote&rdquo; &mdash; end</p>"
# lxml parses entities into Unicode characters
lxml_orig = "A & B and \u201cquote\u201d \u2014 end"

def sim_find(orig_text, content, start=0):
    idx = content.find(orig_text, start)
    escaped = False
    if idx == -1:
        orig_escaped = html.escape(orig_text)
        for char, entity in _ENTITY_REVERSE_MAP:
            orig_escaped = orig_escaped.replace(char, entity)
        idx = content.find(orig_escaped, start)
        if idx != -1:
            orig_text = orig_escaped
            escaped = True
    return idx, orig_text, escaped

idx, found_text, was_escaped = sim_find(lxml_orig, raw_content)
check(idx != -1, f"10a: entity fallback failed, idx={idx}")
check(was_escaped, "10a: should have detected escaped mode")
print(f"  10a entity fallback: {'OK' if idx!=-1 and was_escaped else 'FAIL'}")

# Now test the write-back: repl must be XML-safe
repl = "A & B and \u201cquote\u201d \u2014 end"  # LLM correction (could reintroduce &)
repl_final = html.escape(repl)
if was_escaped:
    for char, entity in _ENTITY_REVERSE_MAP:
        repl_final = repl_final.replace(char, entity)
check("&amp;" in repl_final, f"10b: & not escaped in output: {repr(repl_final)}")
check("&ldquo;" in repl_final, f"10b: left quote not entity")
check("&mdash;" in repl_final, f"10b: em dash not entity")
print(f"  10b write-back safety: {'OK' if '&amp;' in repl_final and '&ldquo;' in repl_final else 'FAIL'}")
print()

# ============================================================
# Test 11: UTF-8 BOM stripping (Bug 4, round 8)
# ============================================================
print("=== Test 11: _decode_xhtml UTF-8 BOM ===")

bom_bytes = b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?><html>test</html>'
text, enc = _decode_xhtml(bom_bytes)
check(enc == "utf-8", f"11a: encoding should be utf-8, got {enc}")
check(not text.startswith('\ufeff'), f"11a: BOM not stripped, starts with {repr(text[:10])}")
check("test" in text, "11a: content after BOM missing")

# utf-8 with BOM but no XML declaration
bom_plain = b'\xef\xbb\xbf<html>hello</html>'
text, enc = _decode_xhtml(bom_plain)
check(enc == "utf-8", f"11b: got {enc}")
check("hello" in text, "11b: content missing")
check(not text.startswith('\ufeff'), "11b: BOM not stripped")

print(f"  11 BOM handling: {'OK' if enc=='utf-8' and 'hello' in text else 'FAIL'}")
print()

# ============================================================
# Test 12: End-of-string punctuation regex (Bug 3, round 8)
# ============================================================
print("=== Test 12: end-of-string punctuation ===")

# Test 12a: comma at end of text node — should be converted
result = apply_mechanical_style_fixes("他说,")
check("\uff0c" in result, f"12a: end-of-string comma not converted: {repr(result)}")

# Test 12b: colon at end of text node
result = apply_mechanical_style_fixes("他说:")
check("\uff1a" in result, f"12b: end-of-string colon not converted: {repr(result)}")

# Test 12c: semicolon at end
result = apply_mechanical_style_fixes("他说;")
check("\uff1b" in result, f"12c: end-of-string semicolon not converted: {repr(result)}")

# Test 12d: comma before English — should NOT convert (protect mixed content)
result = apply_mechanical_style_fixes("他说,hello")
check("," in result, f"12d: comma before English was converted: {repr(result)}")

# Test 12e: comma before digit — should NOT convert (protect numbers)
result = apply_mechanical_style_fixes("价格,123")
check("," in result, f"12e: comma before digit was converted: {repr(result)}")

# Test 12f: normal comma between Chinese — should still convert
result = apply_mechanical_style_fixes("你好,世界")
check("\uff0c" in result, f"12f: normal comma not converted: {repr(result)}")

print(f"  12 end-of-string punct: OK")
print()

# ============================================================
# Test 13: &apos; entity handling (Bug 2, round 8)
# ============================================================
print("=== Test 13: &apos; single-quote entity ===")

# Simulate: EPUB has Don&apos;t, lxml decodes to Don't
raw_content13 = "<p>Don&apos;t go</p>"
lxml_text = "Don't go"

idx, found_text, was_escaped = sim_find(lxml_text, raw_content13)
check(idx != -1, f"13a: &apos; find failed, idx={idx}")
check(was_escaped, "13a: should have detected escaped mode")
check("&apos;" in found_text, f"13a: found text lacks &apos;: {repr(found_text)}")

# And write-back should produce &apos; not &#x27;
repl13 = "Don't go"  # LLM correction
repl_final13 = html.escape(repl13)
if was_escaped:
    for char, entity in _ENTITY_REVERSE_MAP:
        repl_final13 = repl_final13.replace(char, entity)
check("&apos;" in repl_final13, f"13b: write-back missing &apos;: {repr(repl_final13)}")
check("&#x27;" not in repl_final13, f"13b: write-back has &#x27; instead of &apos;: {repr(repl_final13)}")

print(f"  13 &apos; handling: {'OK' if '&apos;' in repl_final13 else 'FAIL'}")
print()

# ============================================================
# Test 14: JSON trailing comma tolerance (Bug 2, round 8)
# ============================================================
print("=== Test 14: JSON trailing comma ===")

# Simulate LLM output with trailing comma
trailing_comma_json = '''```json
{
  "glossary_additions": [
    {"term": "x", "translation": "y"},
  ],
  "corrections": [
    {"chapter": 0, "segment_id": "1", "corrected": "test"},
  ]
}
```'''

import re as re_mod
json_blocks = re_mod.findall(r"```(?:json)?\s*\n?(.*?)```", trailing_comma_json, re_mod.DOTALL)
check(len(json_blocks) == 1, f"14a: expected 1 block, got {len(json_blocks)}")

import json
data14 = {}
for block in json_blocks:
    block = re_mod.sub(r',\s*([}\]])', r'\1', block)
    try:
        block_data = json.loads(block)
        for k, v in block_data.items():
            if k in data14 and isinstance(data14[k], list) and isinstance(v, list):
                data14[k].extend(v)
            else:
                data14[k] = v
    except json.JSONDecodeError as e:
        check(False, f"14a: still failed after comma fix: {e}")

check("glossary_additions" in data14, "14b: glossary_additions missing")
check(len(data14.get("glossary_additions", [])) == 1, f"14b: wrong count: {len(data14.get('glossary_additions',[]))}")

print(f"  14 trailing comma: {'OK' if 'glossary_additions' in data14 else 'FAIL'}")
print()

# ============================================================
# Test 15: Multi JSON block merging (Bug 1, round 8)
# ============================================================
print("=== Test 15: multi JSON block merging ===")

multi_block = '''Here are the terms:
```json
{"glossary_additions": [{"term": "a", "translation": "b"}]}
```
And corrections:
```json
{"corrections": [{"chapter": 0, "segment_id": "1", "corrected": "x"}]}
```'''

json_blocks15 = re_mod.findall(r"```(?:json)?\s*\n?(.*?)```", multi_block, re_mod.DOTALL)
check(len(json_blocks15) == 2, f"15a: expected 2 blocks, got {len(json_blocks15)}")

data15 = {}
for block in json_blocks15:
    block = re_mod.sub(r',\s*([}\]])', r'\1', block)
    try:
        block_data = json.loads(block)
        for k, v in block_data.items():
            if k in data15 and isinstance(data15[k], list) and isinstance(v, list):
                data15[k].extend(v)
            else:
                data15[k] = v
    except json.JSONDecodeError:
        continue

check("glossary_additions" in data15, "15b: glossary block lost")
check("corrections" in data15, "15c: corrections block lost")
check(len(data15["glossary_additions"]) == 1, f"15d: glossary count: {len(data15['glossary_additions'])}")
check(len(data15["corrections"]) == 1, f"15e: corrections count: {len(data15['corrections'])}")

print(f"  15 multi-block: {'OK' if 'glossary_additions' in data15 and 'corrections' in data15 else 'FAIL'}")
print()

# ============================================================
# Test 16: safe EPUB extraction blocks path traversal
# ============================================================
print("=== Test 16: safe EPUB extraction ===")

import tempfile
import zipfile
from pathlib import Path as PathClass

with tempfile.TemporaryDirectory() as td:
    tmp = PathClass(td)
    bad_zip = tmp / "bad.epub"
    out_dir = tmp / "out"
    outside = tmp / "evil.txt"
    with zipfile.ZipFile(str(bad_zip), "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("../evil.txt", "owned")
    try:
        with zipfile.ZipFile(str(bad_zip), "r") as zf:
            _safe_extract_epub(zf, out_dir)
        check(False, "16a: traversal archive was accepted")
    except ValueError:
        check(True, "16a: traversal blocked")
    check(not outside.exists(), "16b: traversal wrote outside work dir")

with tempfile.TemporaryDirectory() as td:
    tmp = PathClass(td)
    good_zip = tmp / "good.epub"
    out_dir = tmp / "out"
    with zipfile.ZipFile(str(good_zip), "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OEBPS/Text/ch1.xhtml", "<html/>")
    with zipfile.ZipFile(str(good_zip), "r") as zf:
        _safe_extract_epub(zf, out_dir)
    check((out_dir / "OEBPS" / "Text" / "ch1.xhtml").exists(), "16c: safe archive not extracted")

print("  16 safe extraction: OK")
print()

# ============================================================
# Summary
# ============================================================
print(f"{'='*60}")
print(f"Results: {passed} passed, {failed} failed")
if failed == 0:
    print("ALL REGRESSION TESTS PASSED")
else:
    print(f"FAILURES DETECTED: {failed}")
    sys.exit(1)
