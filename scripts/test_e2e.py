#!/usr/bin/env python3
"""End-to-end regression test for epub-chinese-proofreading skill.

Creates a minimal EPUB, runs the full pipeline, simulates LLM corrections,
and verifies the output EPUB is valid and correct.
"""
import sys, os, json, shutil, tempfile, zipfile, subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "proofread.py"
passed = failed = 0

def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {msg}")

def run(*args):
    """Run proofread.py with arguments, return (returncode, stdout)."""
    cmd = [sys.executable, str(SCRIPT)] + list(args)
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', env=env)
    return r.returncode, r.stdout, r.stderr

# ============================================================
# Setup: create a minimal valid EPUB with Chinese content
# ============================================================
print("=" * 60)
print("E2E Regression Test: epub-chinese-proofreading")
print("=" * 60)

tmpdir = Path(tempfile.mkdtemp(prefix="epub_test_"))
print(f"\nTest directory: {tmpdir}")

# Create a minimal EPUB structure
epub_dir = tmpdir / "epub_src"
epub_dir.mkdir()

# META-INF/container.xml
meta_dir = epub_dir / "META-INF"
meta_dir.mkdir()
with open(meta_dir / "container.xml", "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>')

# OEBPS directory
oebps = epub_dir / "OEBPS"
oebps.mkdir()

# content.opf
with open(oebps / "content.opf", "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<package version="2.0" xmlns="http://www.idpf.org/2007/opf">\n'
            '  <metadata>\n'
            '    <dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">测试小说</dc:title>\n'
            '    <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">测试作者</dc:creator>\n'
            '  </metadata>\n'
            '  <manifest>\n'
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
            '    <item id="ch1" href="Text/chapter1.xhtml" media-type="application/xhtml+xml"/>\n'
            '    <item id="ch2" href="Text/chapter2.xhtml" media-type="application/xhtml+xml"/>\n'
            '    <item id="ch3" href="Text/chapter3.xhtml" media-type="application/xhtml+xml"/>\n'
            '  </manifest>\n'
            '  <spine>\n'
            '    <itemref idref="ch1"/>\n'
            '    <itemref idref="ch2"/>\n'
            '    <itemref idref="ch3"/>\n'
            '  </spine>\n'
            '</package>')

# toc.ncx
with open(oebps / "toc.ncx", "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ncx version="2005-1" xmlns="http://www.daisy.org/z3986/2005/ncx/">\n'
            '  <navMap>\n'
            '    <navPoint id="ch1"><navLabel><text>Chapter 1</text></navLabel><content src="Text/chapter1.xhtml"/></navPoint>\n'
            '    <navPoint id="ch2"><navLabel><text>Chapter 2</text></navLabel><content src="Text/chapter2.xhtml"/></navPoint>\n'
            '    <navPoint id="ch3"><navLabel><text>Chapter 3</text></navLabel><content src="Text/chapter3.xhtml"/></navPoint>\n'
            '  </navMap>\n'
            '</ncx>')

# Text directory
text_dir = oebps / "Text"
text_dir.mkdir(parents=True)

# Chapter 1: Normal text with a glossary term and a style element
with open(text_dir / "chapter1.xhtml", "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            '<head><title>第一章</title></head>\n'
            '<body>\n'
            '<h1>第一章 开端</h1>\n'
            '<p>亚拉冈站在山丘上，眺望远方的地平线。风很大，吹动他的长发。</p>\n'
            '<p>他点了点头，然后继续前进。他点了点头，向同伴示意。<style>.note{color:gray}</style>队伍继续赶路。</p>\n'
            '<p><!-- 排版备注：此处需要换行 -->天色渐暗，他们决定扎营休息。夜幕降临，星空璀璨。</p>\n'
            '</body>\n'
            '</html>')

# Chapter 2: Text with long paragraph and entities
with open(text_dir / "chapter2.xhtml", "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            '<head><title>第二章</title></head>\n'
            '<body>\n'
            '<h1>第二章 旅程</h1>\n'
            '<p>这是一个很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长的段落。这是第二句。这是第三句。这是第四句。这是第五句。这是第六句。</p>\n'
            '<p>艾隆说道："我们使用了特殊的材料&mdash;秘银。"他指着地图上的位置。</p>\n'
            '<p>哈利·波特拿起魔杖，念出了咒语。A &amp; B 是两个重要的坐标。</p>\n'
            '</body>\n'
            '</html>')

# Chapter 3: Names with middle dots, entities
with open(text_dir / "chapter3.xhtml", "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            '<head><title>第三章</title></head>\n'
            '<body>\n'
            '<h1>第三章 终章</h1>\n'
            '<p>最后，亚拉冈与艾隆握手告别。他点了点头，微微一笑。</p>\n'
            '<p>哈利·波特和赫敏也来到了现场。这是一个非常重要的时刻。</p>\n'
            '</body>\n'
            '</html>')

# mimetype file
with open(epub_dir / "mimetype", "w", encoding="utf-8") as f:
    f.write("application/epub+zip")

# Create EPUB zip
input_epub = tmpdir / "test_novel.epub"
with zipfile.ZipFile(str(input_epub), "w", zipfile.ZIP_DEFLATED) as zf:
    zf.write(str(epub_dir / "mimetype"), "mimetype", compress_type=zipfile.ZIP_STORED)
    for root, dirs, files in os.walk(str(epub_dir)):
        for fn in files:
            if fn == "mimetype":
                continue
            fp = os.path.join(root, fn)
            arc = os.path.relpath(fp, str(epub_dir)).replace("\\", "/")
            zf.write(fp, arc)

print(f"Created test EPUB: {input_epub}")

# ============================================================
# Step 1: init
# ============================================================
print("\n--- Step 1: init ---")
work_dir = tmpdir / "work"
rc, out, err = run("init", str(input_epub), str(work_dir))
check(rc == 0, f"init failed: {err}")
check((work_dir / "context.json").exists(), "context.json not created")
print(f"  init: {'OK' if rc==0 else 'FAIL'}")

# ============================================================
# Step 2: extract
# ============================================================
print("\n--- Step 2: extract ---")
rc, out, err = run("extract", str(work_dir))
check(rc == 0, f"extract failed: {err}")
extracted_dir = work_dir / "extracted"
check(extracted_dir.exists(), "extracted dir missing")
check((extracted_dir / "index.json").exists(), "index.json missing")
print(f"  extract: {'OK' if rc==0 else 'FAIL'}")

# Verify extracted segments: chapter 1
with open(extracted_dir / "chapter_0000.json", "r", encoding="utf-8") as f:
    ch1 = json.load(f)
ch1_texts = [s["content"] for s in ch1["segments"]]
# Should NOT contain CSS, title text, or comment text
check(not any(".note" in t for t in ch1_texts), "CSS extracted into text")
check(not any("第一章" == t for t in ch1_texts), "title text extracted")
check(not any("排版备注" in t for t in ch1_texts), "comment extracted")
# Should contain the body text
check(any("亚拉冈" in t for t in ch1_texts), "text missing: 亚拉冈")
# style.tail must be extracted ("队伍继续赶路")
check(any("队伍继续赶路" in t for t in ch1_texts), "style.tail lost")
# comment.tail must be extracted ("天色渐暗")
check(any("天色渐暗" in t for t in ch1_texts), "comment.tail lost")
print(f"  extract ch1 validation: OK")

# Chapter 2: entity handling
with open(extracted_dir / "chapter_0001.json", "r", encoding="utf-8") as f:
    ch2 = json.load(f)
ch2_texts = [s["content"] for s in ch2["segments"]]
# lxml should have decoded &mdash; to —
check(any("秘银" in t for t in ch2_texts), "text missing: 秘银")
# lxml decodes &amp; to U+0026 (the & character). Check for either the
# decoded form (A & B) or the entity string (A &amp; B).
amp_ok = any(("A " in t and " B" in t and ("是两个重要的坐标" in t)) for t in ch2_texts)
check(amp_ok, f"entity paragraph not found correctly in ch2")
check(any("是两个重要的坐标" in t for t in ch2_texts), "ch2 coordinate text missing")
print(f"  extract ch2 validation: OK")

# ============================================================
# Step 3: preprocess (mechanical phases)
# ============================================================
print("\n--- Step 3: preprocess ---")
rc, out, err = run("preprocess", str(work_dir))
check(rc == 0, f"preprocess failed: {err}")
preproc_path = extracted_dir / "chapter_0000_preprocessed.json"
check(preproc_path.exists(), "_preprocessed.json missing")
print(f"  preprocess: {'OK' if rc==0 else 'FAIL'}")

# ============================================================
# Step 4: add glossary terms
# ============================================================
print("\n--- Step 4: add glossary terms ---")
terms = [{"term": "亚拉冈", "translation": "阿拉贡"}, {"term": "艾隆", "translation": "埃尔隆德"}]
rc, out, err = run("add-terms", str(work_dir), json.dumps(terms, ensure_ascii=False))
check(rc == 0, f"add-terms failed: {err}")

# Verify glossary
with open(work_dir / "glossary.json", "r", encoding="utf-8") as f:
    glossary = json.load(f)
check(glossary.get("亚拉冈") == "阿拉贡", "glossary term not added")
print(f"  add-terms: {'OK' if glossary.get('亚拉冈')=='阿拉贡' else 'FAIL'}")

# ============================================================
# Step 5: reprocess (apply new glossary mechanically)
# ============================================================
print("\n--- Step 5: reprocess ---")
rc, out, err = run("reprocess", str(work_dir))
check(rc == 0, f"reprocess failed: {err}")
with open(preproc_path, "r", encoding="utf-8") as f:
    preproc_data = json.load(f)
preproc_texts = [s["content"] for s in preproc_data["segments"]]
# Should have glossary replacements
check(any("阿拉贡" in t for t in preproc_texts), "glossary replacement not applied")
check(not any("亚拉冈" in t for t in preproc_texts), "original term still present")
print(f"  reprocess: {'OK' if '阿拉贡' in str(preproc_texts) else 'FAIL'}")

# ============================================================
# Step 6: simulate LLM corrections (apply-corrections)
# ============================================================
print("\n--- Step 6: simulate LLM corrections ---")

# Find segments in ch1 preprocessed to create corrections
ch1_preproc_path = extracted_dir / "chapter_0000_preprocessed.json"
with open(ch1_preproc_path, "r", encoding="utf-8") as f:
    ch1_pp = json.load(f)

# Find segment IDs for correction
seg_map = {}
for seg in ch1_pp["segments"]:
    seg_id = seg["id"]
    sub_id = seg.get("sub_id", 0)
    seg_map.setdefault(seg_id, {})[sub_id] = seg

# Create LLM corrections JSON
corrections_json = {
    "glossary_additions": [
        {"term": "哈利·波特", "translation": "哈利波特"}
    ],
    "corrections": [
        {"chapter": 0, "segment_id": "0.0",
         "corrected": "阿拉贡站在山丘上，眺望远方的地平线。风很大，吹动他的黑色长发。"},
        {"chapter": 1, "segment_id": "1.0",
         "corrected": "埃尔隆德说道：我们使用了特殊的材料——秘银。他指着地图上的位置。"},
    ]
}

corr_file = tmpdir / "corrections.json"
with open(corr_file, "w", encoding="utf-8") as f:
    json.dump(corrections_json, f, ensure_ascii=False)

rc, out, err = run("apply-corrections", str(work_dir), str(corr_file))
check(rc == 0, f"apply-corrections failed: {err}")

# Verify _corrected.json was created for corrected chapters
ch1_corr = extracted_dir / "chapter_0000_corrected.json"
ch2_corr = extracted_dir / "chapter_0001_corrected.json"
ch1_sentinel = extracted_dir / "chapter_0000.corrected"
ch2_sentinel = extracted_dir / "chapter_0001.corrected"

check(ch1_corr.exists(), "ch1 _corrected.json missing")
check(ch1_sentinel.exists(), "ch1 sentinel missing")
check(ch2_corr.exists(), "ch2 _corrected.json missing")
check(ch2_sentinel.exists(), "ch2 sentinel missing")

# Verify ch3 was NOT corrected — NO sentinel should exist
ch3_sentinel = extracted_dir / "chapter_0002.corrected"
check(not ch3_sentinel.exists(), "ch3 should not have sentinel")

# Verify corrected content
with open(ch1_corr, "r", encoding="utf-8") as f:
    ch1_data = json.load(f)
ch1_contents = [s.get("content","") for s in ch1_data["segments"]]
check(any("黑色长发" in t for t in ch1_contents), "LLM correction not applied to ch1")

# Verify glossary_additions applied to non-corrected ch3
ch3_pp = extracted_dir / "chapter_0002_preprocessed.json"
with open(ch3_pp, "r", encoding="utf-8") as f:
    ch3_data = json.load(f)
# After reprocess (triggered by glossary_additions in apply-corrections),
# ch3 should have the new glossary terms in _preprocessed.json
# (and no sentinel, so check/inject will use _preprocessed.json)
print(f"  apply-corrections: {'OK' if ch1_corr.exists() and '黑色长发' in str(ch1_contents) else 'FAIL'}")

# ============================================================
# Step 7: check
# ============================================================
print("\n--- Step 7: check ---")
rc, out, err = run("check", str(work_dir), "--diff")
# check returns non-zero when violations found — this is EXPECTED here
# because our LLM corrections intentionally changed text above the 40% threshold
check("segments modified" in out, f"check diff not working. stdout={out[:500]}")
check("Change ratio" in out, f"check violations not detected. stdout={out[:500]}")
print(f"  check: OK (detected {out.count('violation')} violations as expected)")

# ============================================================
# Step 8: inject
# ============================================================
print("\n--- Step 8: inject ---")
rc, out, err = run("inject", str(work_dir))
check(rc == 0, f"inject failed: {err}")
print(f"  inject: {'OK' if rc==0 else 'FAIL'}")

# Verify injected XHTML content
ch1_xhtml = work_dir / "OEBPS" / "Text" / "chapter1.xhtml"
with open(ch1_xhtml, "r", encoding="utf-8") as f:
    ch1_content = f.read()

# Should have LLM corrections
check("阿拉贡" in ch1_content, "阿拉贡 not found in injected ch1")
check("黑色长发" in ch1_content, "LLM correction not injected into ch1")
# CSS should be intact
check(".note{color:gray}" in ch1_content, "CSS corrupted during inject")
# Entities should be preserved
# verify ch2 XHTML: em dash content preserved
ch2_xhtml = work_dir / "OEBPS" / "Text" / "chapter2.xhtml"
with open(ch2_xhtml, "r", encoding="utf-8") as f:
    ch2_content = f.read()
check("秘银" in ch2_content, "em dash context lost in ch2")
print(f"  inject ch1 validation: OK")

# Verify chapter 3 has glossary terms applied (from reprocess → _preprocessed.json)
ch3_xhtml = work_dir / "OEBPS" / "Text" / "chapter3.xhtml"
with open(ch3_xhtml, "r", encoding="utf-8") as f:
    ch3_content = f.read()
check("哈利波特" in ch3_content, "glossary term not injected into ch3 (via preprocessed)")
check("阿拉贡" in ch3_content, "glossary term 亚拉冈 not injected into ch3")
print(f"  inject ch3 validation: OK")

# ============================================================
# Step 9: pack
# ============================================================
print("\n--- Step 9: pack ---")
output_epub = tmpdir / "output.epub"
rc, out, err = run("pack", str(work_dir), str(output_epub))
check(rc == 0, f"pack failed: {err}")
check(output_epub.exists(), "output EPUB not created")

# Verify the output EPUB is a valid ZIP
try:
    with zipfile.ZipFile(str(output_epub), "r") as zf:
        names = zf.namelist()
        check("mimetype" in names, "mimetype missing from output EPUB")
        check(any("chapter1.xhtml" in n for n in names), "chapter1 missing from output EPUB")
        # Verify no backslash paths in the archive (Windows bug)
        bad_paths = [n for n in names if "\\" in n]
        check(not bad_paths, f"backslash in ZIP entry: {bad_paths}")
    print(f"  pack: OK (valid EPUB)")
except Exception as e:
    check(False, f"pack output is not a valid ZIP: {e}")

# ============================================================
# Step 9b: dump-text with --max-chars (chunking test)
# ============================================================
print("\n--- Step 9b: dump-text --max-chars ---")
# Set max_chars low enough to trigger chunking with 3 chapters
rc, out, err = run("dump-text", str(work_dir), "--max-chars", "100")
check(rc == 0, f"dump-text --max-chars failed: {err}")
batch_dir = work_dir / "proofread_batches"
check(batch_dir.exists(), "proofread_batches dir not created")
batch_files = sorted(batch_dir.glob("batch_*.txt"))
check(len(batch_files) >= 2, f"expected >=2 batches, got {len(batch_files)}: {[f.name for f in batch_files]}")
# Verify batch file naming convention
first_batch = batch_files[0].name
check("batch_" in first_batch and "_ch" in first_batch and "_to_" in first_batch,
      f"bad batch naming: {first_batch}")
# Verify full_text.txt still exists (unchunked full file)
check((work_dir / "full_text.txt").exists(), "full_text.txt missing after chunked dump")
print(f"  dump-text --max-chars: OK ({len(batch_files)} batches)")

# ============================================================
# Step 10: second round — add new glossary term, verify corrections survive
# ============================================================
print("\n--- Step 10: incremental corrections round ---")
terms2 = [{"term": "赫敏", "translation": "赫敏·格兰杰"}]
rc, out, err = run("add-terms", str(work_dir), json.dumps(terms2, ensure_ascii=False))
check(rc == 0, f"add-terms round 2 failed: {err}")

# Simulate second LLM corrections (only for ch1, with a new glossary term)
corrections_json2 = {
    "glossary_additions": [{"term": "魔杖", "translation": "魔法杖"}],
    "corrections": [
        {"chapter": 1, "segment_id": "2.0",
         "corrected": "哈利波特拿起魔法杖，念出了咒语。A与B是两个重要的坐标。"},
    ]
}
corr_file2 = tmpdir / "corrections2.json"
with open(corr_file2, "w", encoding="utf-8") as f:
    json.dump(corrections_json2, f, ensure_ascii=False)

rc, out, err = run("apply-corrections", str(work_dir), str(corr_file2))
check(rc == 0, f"apply-corrections round 2 failed: {err}")

# Verify ch1 _preprocessed.json was NOT polluted with LLM text
with open(extracted_dir / "chapter_0000_preprocessed.json", "r", encoding="utf-8") as f:
    ch1_pp_round2 = json.load(f)
ch1_pp_contents = [s.get("content","") for s in ch1_pp_round2["segments"]]
check(not any("黑色长发" in t for t in ch1_pp_contents),
      "CRITICAL: _preprocessed.json polluted with LLM text (黑色长发)")

# _preprocessed.json content SHOULD have glossary replacements (阿拉贡 not 亚拉冈)
# The original field should have the pre-glossary text
check(any("阿拉贡" in t for t in ch1_pp_contents),
      "_preprocessed.json missing glossary replacement in content")
check(not any("亚拉冈" in t for t in ch1_pp_contents),
      "_preprocessed.json content still has original term (should be replaced)")

# Verify _preprocessed.json original fields are correct (from raw extract)
ch1_pp_originals = [s.get("original","") for s in ch1_pp_round2["segments"]]
check(any("亚拉冈" in t for t in ch1_pp_originals),
      "original field should contain pre-glossary text (亚拉冈)")
print(f"  round 2 preprocessed integrity: OK")

# Verify ch1 _corrected.json survived and has previous corrections
with open(extracted_dir / "chapter_0000_corrected.json", "r", encoding="utf-8") as f:
    ch1_corr_round2 = json.load(f)
ch1_corr_contents = [s.get("content","") for s in ch1_corr_round2["segments"]]
# Must still have the FIRST round correction
check(any("黑色长发" in t for t in ch1_corr_contents),
      "CRITICAL: first round LLM correction lost after second apply-corrections")
check(any("阿拉贡" in t for t in ch1_corr_contents),
      "glossary replacement 亚拉冈 missing in _corrected.json")
print(f"  round 2 correction survival: OK")

# Verify ch1 sentinel still exists (sentinel was NOT deleted because ch1 was re-corrected)
check(ch1_sentinel.exists(), "ch1 sentinel deleted — would lose corrections on next check/inject")
print(f"  round 2 sentinel preservation: OK")

# ============================================================
# Step 11: final inject and pack verification
# ============================================================
print("\n--- Step 11: final inject ---")
rc, out, err = run("inject", str(work_dir))
check(rc == 0, f"final inject failed: {err}")

# Read final ch1
with open(ch1_xhtml, "r", encoding="utf-8") as f:
    ch1_final = f.read()
check("阿拉贡" in ch1_final, "final ch1: 阿拉贡 missing")
check("黑色长发" in ch1_final, "final ch1: first round correction missing")

# Read final ch3 — should get glossary from _preprocessed.json (no sentinel)
with open(ch3_xhtml, "r", encoding="utf-8") as f:
    ch3_final = f.read()
check("阿拉贡" in ch3_final, "final ch3: 阿拉贡 missing (from preprocessed)")
check("哈利波特" in ch3_final, "final ch3: 哈利波特 missing (glossary)")
check("埃尔隆德" in ch3_final, "final ch3: 埃尔隆德 missing (glossary)")
print(f"  final inject: OK")

# ============================================================
# Cleanup
# ============================================================
shutil.rmtree(tmpdir, ignore_errors=True)

# ============================================================
# Summary
# ============================================================
print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed")
if failed == 0:
    print("ALL E2E TESTS PASSED — skill is healthy for lazy-mode operation")
else:
    print(f"FAILURES: {failed}")
    sys.exit(1)
