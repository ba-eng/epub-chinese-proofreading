#!/usr/bin/env python3
"""Simulate the full SKILL.md workflow: pipeline -> correct -> check -> inject -> pack."""
import tempfile, subprocess, sys, os, zipfile, shutil, json
from pathlib import Path

PROOFREAD_PY = str(Path(__file__).resolve().parent / "proofread.py")

print("=" * 60)
print('SKILL WORKFLOW SIMULATION')
print("=" * 60)

tmp = Path(tempfile.mkdtemp(prefix="skill_test_"))
edir = tmp / "src"
oedir = edir / "OEBPS" / "Text"
oedir.mkdir(parents=True)
(edir / "META-INF").mkdir(parents=True)

# Create test EPUB with inline style, long paragraph, entities, comments
with open(edir / "mimetype", "w") as f: f.write("application/epub+zip")
with open(edir / "META-INF" / "container.xml", "w") as f:
    f.write('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
with open(edir / "OEBPS" / "content.opf", "w") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?><package version="2.0" xmlns="http://www.idpf.org/2007/opf"><metadata><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">Skill Test</dc:title></metadata><manifest><item id="c1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="Text/ch2.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
with open(oedir / "ch1.xhtml", "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head><title>C1</title></head><body><h1>Chapter 1</h1><p>亚拉冈站在山丘上眺望远方。风很大，吹动他的长发。<style>.note{color:gray}</style>他点了点头，继续前行。</p><p><!-- comment -->天色渐暗，远处有火光闪烁。A &amp; B 是两个坐标。</p></body></html>')
with open(oedir / "ch2.xhtml", "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head><title>C2</title></head><body><h1>Chapter 2</h1><p>第二天，亚拉冈醒来时太阳已经升起。这是一个很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长的段落。这是第二句。这是第三句。</p></body></html>')

epub = tmp / "test.epub"
with zipfile.ZipFile(str(epub), "w", zipfile.ZIP_DEFLATED) as zf:
    zf.write(str(edir / "mimetype"), "mimetype", zipfile.ZIP_STORED)
    for root, dirs, files in os.walk(str(edir)):
        for fn in files:
            if fn == "mimetype": continue
            fp = os.path.join(root, fn)
            zf.write(fp, os.path.relpath(fp, str(edir)).replace("\\", "/"))

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"
wdir = str(tmp / "work")

# ====== STEP 1: pipeline ======
print("\n[STEP 1] pipeline")
r = subprocess.run([sys.executable, PROOFREAD_PY, "pipeline", str(epub), "--work-dir", wdir],
                   capture_output=True, encoding="utf-8", env=env)
assert r.returncode == 0, f"Pipeline FAILED: {r.stderr}"
task = Path(wdir) / "TASK.md"
ft = Path(wdir) / "full_text.txt"
assert task.exists(), "TASK.md missing"
assert ft.exists(), "full_text.txt missing"
with open(Path(wdir) / "context.json", encoding="utf-8") as f:
    ctx = json.load(f)
assert Path(ctx["project_dir"]) == Path(wdir).parent.resolve(), "explicit --work-dir project_dir should be work_dir parent"
print(f"  project_dir: {ctx['project_dir']}")
print(f"  TASK.md: {len(task.read_text(encoding='utf-8'))} chars")
print(f"  full_text.txt: {len(ft.read_text(encoding='utf-8'))} chars")

# Check CSS NOT in dump, tail IS in dump
content = ft.read_text(encoding="utf-8")
assert ".note" not in content, "CSS leaked into dump"
assert "comment" not in content, "Comment leaked into dump"
assert "他点了点头" in content, "tail after style missing"
print("  Filters: CSS absent, tail present, comment absent")

# ====== STEP 2: LLM corrections (simulate Claude) ======
print("\n[STEP 2] LLM corrections (2 rounds)")

# Round 1
c1 = {
    "glossary_additions": [{"term": "亚拉冈", "translation": "阿拉贡"}],
    "corrections": [{"chapter": 0, "segment_id": 0, "corrected": "阿拉贡站在山丘上眺望远方。风很大，吹动他的长发。他点了点头，继续前行。"}]
}
c1_path = str(Path(wdir) / "corrections_r1.json")
json.dump(c1, open(c1_path, "w", encoding="utf-8"), ensure_ascii=False)
r = subprocess.run([sys.executable, PROOFREAD_PY, "apply-corrections", wdir, c1_path],
                   capture_output=True, encoding="utf-8", env=env)
assert r.returncode == 0, f"apply r1 FAILED: {r.stderr}"
assert (Path(wdir) / "extracted" / "chapter_0000.corrected").exists(), "Sentinel missing r1"
print("  Round 1: OK (sentinel created)")

# Round 2: new term + correction for ch2
c2 = {
    "glossary_additions": [{"term": "长发", "translation": "长发"}],
    "corrections": [{"chapter": 1, "segment_id": 0, "corrected": "第二天，阿拉贡醒来时太阳已经升起。这是一个很长的段落。这是第二句。这是第三句。"}]
}
c2_path = str(Path(wdir) / "corrections_r2.json")
json.dump(c2, open(c2_path, "w", encoding="utf-8"), ensure_ascii=False)
r = subprocess.run([sys.executable, PROOFREAD_PY, "apply-corrections", wdir, c2_path],
                   capture_output=True, encoding="utf-8", env=env)
assert r.returncode == 0, f"apply r2 FAILED: {r.stderr}"

# Verify round 1 corrections survived
with open(Path(wdir) / "extracted" / "chapter_0000_corrected.json", encoding="utf-8") as f:
    ch1_data = json.load(f)
ch1_content = " ".join(s.get("content", "") for s in ch1_data["segments"])
assert "阿拉贡" in ch1_content, "Round 1 correction lost after round 2!"
print("  Round 2: OK (round 1 corrections survived)")

# ====== STEP 3: check + inject + pack ======
print("\n[STEP 3] check --diff + inject + pack")
r = subprocess.run([sys.executable, PROOFREAD_PY, "check", "--diff", wdir],
                   capture_output=True, encoding="utf-8", env=env)
print(f"  check --diff: rc={r.returncode} (violations expected)")

r = subprocess.run([sys.executable, PROOFREAD_PY, "inject", wdir],
                   capture_output=True, encoding="utf-8", env=env)
assert r.returncode == 0, f"Inject FAILED: {r.stderr}"
print("  inject: OK")

r = subprocess.run([sys.executable, PROOFREAD_PY, "pack", wdir],
                   capture_output=True, encoding="utf-8", env=env)
assert r.returncode == 0, f"Pack FAILED: {r.stderr}"
print("  pack: OK")

# ====== VERIFY ======
print("\n[VERIFY]")
epubs = list(Path(wdir).rglob("*.epub"))
if not epubs:
    epubs = list(Path(wdir).parent.glob("*.epub"))
assert epubs, "No output EPUB"
assert Path(wdir).parent / "output.epub" in epubs, "pack output should use explicit --work-dir parent"
with zipfile.ZipFile(str(epubs[0])) as zf:
    names = zf.namelist()
    assert "mimetype" in names, "Invalid EPUB"
    assert not any("\\" in n for n in names), "Backslash in ZIP"
print(f"  Valid EPUB: {epubs[0]}")

for f in Path(wdir).rglob("ch1.xhtml"):
    text = f.read_text(encoding="utf-8")
    assert "阿拉贡" in text, "ch1: glossary not injected"
    assert ".note{color:gray}" in text, "ch1: CSS corrupted"
    print("  ch1: glossary injected, CSS intact")
for f in Path(wdir).rglob("ch2.xhtml"):
    text = f.read_text(encoding="utf-8")
    assert "阿拉贡" in text, "ch2: glossary not injected"
    print("  ch2: glossary injected")

print("\n" + "=" * 60)
print("SKILL WORKFLOW: ALL STEPS PASSED")
print("=" * 60)
shutil.rmtree(tmp, ignore_errors=True)
