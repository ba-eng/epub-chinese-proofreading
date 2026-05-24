#!/usr/bin/env python3
"""
EPUB Chinese Proofreading — Mechanical Engine (Python side)

Architecture: Python = file I/O, LLM = language judgment
  Python: EPUB unzip/pack, text extract/inject, glossary replace,
          punctuation fix, blacklist flag, binary inject, change check
  LLM:   entity extraction, inconsistency detection, boundary analysis,
          style unification, translation腔 fix, context-aware blacklist replacement

Commands:
  pipeline, init, extract, preprocess, reprocess, dump-text, apply-corrections,
  add-term, add-terms, check, extract-terms, inject, pack, config
"""

import argparse
import difflib
import html
import json
import math
import os
import re
import shutil
import sys
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = SKILL_DIR / "scripts" / "config.default.json"
BLACKLIST_DIR = SKILL_DIR / "blacklists"
GLOSSARY_FILENAME = "glossary.json"
FAILED_LIST_FILENAME = "failed_list.txt"
CONFIG_FILENAME = "config.json"

# Reverse entity map: lxml-decoded characters → EPUB named entities.
# html.escape() only handles < > & " ' — these cover the rest.
_ENTITY_REVERSE_MAP = [
    ('\xa0', '&nbsp;'),     # non-breaking space
    ('\u201c', '&ldquo;'),   # left double quote
    ('\u201d', '&rdquo;'),   # right double quote
    ('\u2018', '&lsquo;'),   # left single quote
    ('\u2019', '&rsquo;'),   # right single quote
    ('\u2014', '&mdash;'),   # em dash
    ('\u2013', '&ndash;'),   # en dash
    ('\u2026', '&hellip;'),  # ellipsis
    ('\u00ab', '&laquo;'),   # left angle quote
    ('\u00bb', '&raquo;'),   # right angle quote
    # html.escape artifacts → XML named entities
    ('&#x27;', '&apos;'),    # Python single-quote → EPUB/XML single-quote
]

# ---------------------------------------------------------------------------
# Blacklist profile loading
# ---------------------------------------------------------------------------

BLACKLIST_PROFILES = ["fantasy", "romance", "general", "minimal"]


def _extract_blacklist_words(data):
    """Extract hard blacklist words from a blacklist JSON structure.

    Handles both {"words": [...]} / {"blacklist": [...]} dicts and
    plain [...] arrays (list input that would otherwise crash on .get()).
    """
    if isinstance(data, list):
        return data
    return data.get("words", data.get("blacklist", []))


def _extract_blacklist_advisory(data):
    """Extract soft advisory blacklist words from a blacklist JSON structure."""
    if isinstance(data, list):
        return []
    return data.get("advisory", data.get("blacklist_advisory", []))


def load_blacklist_terms(profile="fantasy", custom_file=None):
    """Load hard and advisory blacklist words from profile/config."""
    if custom_file and os.path.exists(custom_file):
        fp = Path(custom_file)
        if fp.suffix == ".json":
            with open(fp, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return _extract_blacklist_words(data), _extract_blacklist_advisory(data)
        with open(fp, "r", encoding="utf-8-sig") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")], []

    profile_path = BLACKLIST_DIR / f"{profile}.json"
    if profile_path.exists():
        with open(profile_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return _extract_blacklist_words(data), _extract_blacklist_advisory(data)

    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return _extract_blacklist_words(data), _extract_blacklist_advisory(data)

    return [], []


def load_blacklist(profile="fantasy", custom_file=None):
    """Load hard blacklist words from a named profile or custom file."""
    return load_blacklist_terms(profile, custom_file)[0]


# ---------------------------------------------------------------------------
# XML parsing helpers (lxml with fallback to ElementTree)
# ---------------------------------------------------------------------------

def _get_xml_parser():
    """Return (parse_func, etree_module). Prefer lxml with recover=True."""
    try:
        from lxml import etree
        def parse(filepath):
            return etree.parse(str(filepath), etree.XMLParser(recover=True))
        return parse, etree
    except ImportError:
        import xml.etree.ElementTree as ET
        def parse(filepath):
            return ET.parse(str(filepath))
        return parse, ET


def parse_xhtml(filepath):
    """Parse XHTML file, return (tree, root, etree_module)."""
    parse, etree_mod = _get_xml_parser()
    try:
        tree = parse(filepath)
        root = tree.getroot()
        return tree, root, etree_mod
    except Exception:
        # Fallback: read raw bytes and try lxml recover parser
        with open(filepath, "rb") as f:
            raw = f.read()
        try:
            from lxml import etree as letree
        except ImportError:
            raise RuntimeError(
                f"Cannot parse {filepath}: lxml not installed and stdlib parser failed. "
                f"Install lxml: pip install lxml"
            )
        # Decode raw bytes using encoding probe — raw GBK without XML
        # declaration would crash fromstring() otherwise.
        content, _ = _decode_xhtml(raw)
        root = letree.fromstring(content.encode("utf-8"), letree.XMLParser(recover=True))
        tree = root.getroottree()
        return tree, root, letree


def _decode_xhtml(raw):
    """Decode XHTML bytes with encoding detection.

    Tries (in order): XML declaration encoding → utf-8 → gbk.
    Returns (text, encoding_name) tuple for roundtrip write-back.
    """
    # Try to extract encoding from XML declaration: <?xml encoding="XXX"?>
    head = raw[:200].decode("ascii", errors="ignore")
    m = re.search(r'encoding\s*=\s*["\']([^"\']+)["\']', head)
    declared = m.group(1).lower() if m else None

    # Build priority list: declared encoding (if any), then utf-8-sig (strips
    # BOM), then utf-8, then gbk. utf-8-sig handles Windows-generated XHTML
    # files with \xef\xbb\xbf prefix that would crash lxml.
    candidates = []
    if declared and declared != "utf-8" and declared != "utf-8-sig":
        candidates.append(declared)
    candidates.extend(["utf-8-sig", "utf-8", "gbk", "big5"])

    for enc in candidates:
        try:
            decoded = raw.decode(enc)
            # utf-8-sig strips BOM on decode but re-adds it on encode.
            # Report as plain utf-8 to avoid injecting BOM into output.
            return decoded, "utf-8" if enc == "utf-8-sig" else enc
        except (UnicodeDecodeError, LookupError):
            continue

    # Last resort: replace errors
    return raw.decode("utf-8", errors="replace"), "utf-8"


# ---------------------------------------------------------------------------
# Spine (reading order) helpers
# ---------------------------------------------------------------------------

def find_opf_path(work_dir):
    """Find the OPF file per EPUB spec: META-INF/container.xml → rglob fallback."""
    work_dir = Path(work_dir)
    # EPUB spec: META-INF/container.xml is the authoritative source
    container_path = work_dir / "META-INF" / "container.xml"
    if container_path.exists():
        try:
            tree, root, etree_mod = parse_xhtml(container_path)
            for rootfile in root.iter():
                tag = (rootfile.tag.split("}")[-1] if "}" in str(rootfile.tag)
                       else str(rootfile.tag))
                if tag == "rootfile":
                    full_path = rootfile.get("full-path")
                    if full_path:
                        # container.xml paths are URIs per OEBPS spec.
                        # Spaces, CJK chars etc. are percent-encoded.
                        opf_path = work_dir / urllib.parse.unquote(full_path)
                        if opf_path.exists():
                            return opf_path
        except Exception:
            pass
    # Fallback: rglob with MACOSX filter
    candidates = [p for p in work_dir.rglob("*.opf")
                   if "__MACOSX" not in p.parts]
    if candidates:
        return candidates[0]
    return None


def read_spine(work_dir, opf_path=None):
    """Read EPUB spine ordering. Returns list of XHTML file paths (relative to work_dir)."""
    if opf_path is None:
        opf_path = find_opf_path(work_dir)
    if opf_path is None:
        return []  # fallback to filesystem order

    try:
        from lxml import etree
        tree = etree.parse(str(opf_path), etree.XMLParser(recover=True))
        root = tree.getroot()
    except Exception:
        return []

    # Namespace handling
    ns = {}
    for m in re.finditer(r'xmlns:?(\w*)=["\']([^"\']+)["\']', etree.tostring(root).decode("utf-8", errors="replace")):
        prefix, uri = m.group(1) or "default", m.group(2)
        ns[prefix] = uri

    # Build manifest {id: href}
    manifest = {}
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "manifest":
            for item in el:
                item_tag = item.tag.split("}")[-1] if "}" in item.tag else item.tag
                if item_tag == "item":
                    mid = item.get("id")
                    href = item.get("href")
                    if mid and href:
                        # OPF spec requires URL-encoded hrefs (e.g. spaces → %20).
                        # Decode to get the actual filesystem path.
                        manifest[mid] = urllib.parse.unquote(href)

    # Read spine order
    spine_order = []
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "spine":
            for itemref in el:
                iref_tag = itemref.tag.split("}")[-1] if "}" in itemref.tag else itemref.tag
                if iref_tag == "itemref":
                    idref = itemref.get("idref")
                    if idref and idref in manifest:
                        spine_order.append(manifest[idref])

    # Resolve relative paths
    opf_dir = opf_path.parent
    resolved = []
    for href in spine_order:
        full = (opf_dir / href).resolve()
        try:
            rel = full.relative_to(work_dir.resolve())
        except ValueError:
            rel = os.path.relpath(str(full), str(work_dir.resolve()))
        resolved.append(str(rel))

    return resolved


def _is_cjk(c):
    """True if c is a CJK character (Basic + Extensions A-G + Compatibility)."""
    return ('\u4e00' <= c <= '\u9fff' or     # CJK Unified Ideographs (BMP)
            '\u3400' <= c <= '\u4dbf' or     # CJK Extension A (BMP)
            '\uf900' <= c <= '\ufaff' or     # CJK Compatibility (BMP)
            '\U00020000' <= c <= '\U0002a6df' or  # Extension B
            '\U0002a700' <= c <= '\U0002b73f' or  # Extension C
            '\U0002b740' <= c <= '\U0002b81f' or  # Extension D
            '\U0002b820' <= c <= '\U0002ceaf' or  # Extension E
            '\U0002ceb0' <= c <= '\U0002ebef' or  # Extension F
            '\U00030000' <= c <= '\U0003134f')    # Extension G


def _is_valid_term_char(c):
    """True if c is a CJK char or a legitimate name connector."""
    if _is_cjk(c):
        return True
    return c in ('·', '\u30fb', '-', ' ', '\u2022', '\u2027')
# U+00B7 middle dot, U+30FB katakana middle dot, hyphen, space, bullet, hyphenation point


def _is_english_heavy(text, check_run=True):
    """Detect English/predominantly-ASCII text.

    Light mode (check_run=False): simple alpha-ratio heuristic. Used for
    protective guards (skip glossary/semicolon processing on likely-English text).

    Full mode (check_run=True): adds long-ASCII-run detection + minimum alpha
    threshold. Used for flagging segments as English content for LLM handling.
    """
    alpha = sum(1 for c in text if c.isalpha())
    eng = sum(1 for c in text if c.isascii() and c.isalpha())
    if not check_run:
        return eng > 0 and eng / max(alpha, 1) > 0.5
    max_ascii_run = 0
    cur_run = 0
    for c in text:
        if c.isascii() and c.isalpha():
            cur_run += 1
            max_ascii_run = max(max_ascii_run, cur_run)
        else:
            cur_run = 0
    return max_ascii_run >= 30 or (
        alpha >= 20 and eng > 0 and eng / max(alpha, 1) > 0.5
    )


def _normalize_segment_id(val, keep_dot_zero=False):
    """Normalize LLM segment_id for dict key lookup.

    LLMs emit 3.0/"3.0" for integer IDs. By default these normalize to "3".
    Set keep_dot_zero=True to preserve "3.0" (needed when data has
    sub-segments and the .0 suffix must match the individual sub-key).
    """
    if keep_dot_zero:
        return str(val)
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    if isinstance(val, str):
        try:
            f = float(val)
            if f.is_integer():
                return str(int(f))
        except (ValueError, TypeError):
            pass
    return str(val)


def natural_sort_key(path):
    """Sort key: chapter2 before chapter10, Volume1/ch1 before Volume2/ch1."""
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split(r'(\d+)', str(path))]


def get_xhtml_files(work_dir, spine_order=None):
    """Get XHTML files in processing order."""
    work_dir = Path(work_dir)
    if spine_order:
        files = []
        for rel in spine_order:
            fp = work_dir / rel
            if fp.exists():
                files.append(fp)
        return files
    # Filesystem fallback — use natural sort (chapter2 before chapter10)
    xhtmls = list(work_dir.rglob("*.xhtml"))
    htmls = list(work_dir.rglob("*.html")) + list(work_dir.rglob("*.htm"))
    return sorted(xhtmls + htmls, key=natural_sort_key)


# ---------------------------------------------------------------------------
# Text segment extraction
# ---------------------------------------------------------------------------

def extract_text_segments(root):
    """Extract all text segments from an XHTML tree.

    Returns list of dicts:
        {"id": int, "type": "text"|"tail", "content": str}

    Processes ALL elements in document order via iter().
    For each element, captures .text (text before first child)
    and .tail (text after closing tag, i.e. sibling text).
    Does NOT capture parent container text that is only whitespace,
    because XHTML parent elements typically have no direct text.
    """
    segments = []
    seg_id = 0

    for element in root.iter():
        # Comment and ProcessingInstruction nodes have .tag as a callable
        # (not a string), so tag-name checks silently fail. Skip .text
        # (comment content is not novel text) but always extract .tail.
        if not isinstance(element.tag, str):
            if element.tail and element.tail.strip():
                segments.append({
                    "id": seg_id,
                    "type": "tail",
                    "content": element.tail
                })
                seg_id += 1
            continue

        # Only skip .text for non-content elements (CSS, JS, metadata).
        # .tail must ALWAYS be extracted — in lxml's data model,
        # <p>正文<style>.bg{}</style>后续正文</p> puts "后续正文" on
        # <style>'s .tail, which belongs to the parent <p>'s content.
        tag_name = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        skip_text = tag_name in ('style', 'script', 'title', 'meta')

        if not skip_text and element.text and element.text.strip():
            segments.append({
                "id": seg_id,
                "type": "text",
                "content": element.text
            })
            seg_id += 1

        # .tail always extracted — it belongs to the parent's content flow
        if element.tail and element.tail.strip():
            segments.append({
                "id": seg_id,
                "type": "tail",
                "content": element.tail
            })
            seg_id += 1

    return segments



# ---------------------------------------------------------------------------
# proofread_text — mechanical phases (A: glossary, B: blacklist)
# ---------------------------------------------------------------------------

def _build_glossary_regex(glossary):
    """Build a compiled regex from glossary terms, longest-first.

    Longest terms first ensures that "房间里" matches before "房" or "门",
    preventing substring conflicts. Returns None if glossary is empty.

    Also injects self-referencing entries for translation values that are
    superstrings of their own keys (prefix-subset problem). Without this,
    a key like "艾格勒莫"→"艾格勒莫特" would corrupt the canonical form
    "艾格勒莫特" into "艾格勒莫特特" when the key matches within its own
    translation during single-pass regex replacement.
    """
    if not glossary:
        return None
    # Add self-entries for prefix-subset targets to prevent corruption.
    # When key K is a proper prefix of its translation V, V must appear
    # in the regex as a self-match (sorted before K by length) so that
    # V is consumed by the longer match before K can match within V.
    patched = dict(glossary)
    for k, v in glossary.items():
        if k != v and v.startswith(k):
            patched.setdefault(v, v)
    # Sort by term length descending — longest match wins
    sorted_terms = sorted(patched.keys(), key=len, reverse=True)
    escaped = [re.escape(t) for t in sorted_terms]
    pattern = '|'.join(escaped)
    return re.compile(pattern)


def proofread_text(text, glossary, blacklist, quote_state=None, advisory_blacklist=None):
    """Apply proofreading phases A & B mechanically.

    Phase A: Glossary replacement — regex-based, longest-term-first.

    Phase A2: Semicolon (；) → comma (，) — mechanical replacement;
      Chinese fiction uses semicolons extremely rarely; most are
      English translation artifacts.

    Phase B: Blacklist FLAGGING (NOT skipping).
      Blacklist = undesirable网文 clichés that need to be replaced.
      Advisory blacklist = context-dependent words that should be reviewed
      but do not count as hard violations or trigger auto-correction.

    Args:
        text: Original text string
        glossary: dict of {original_term: unified_translation}
        blacklist: list of网文词汇 needing treatment
        quote_state: Optional dict {"left": bool} to persist quote parity
                     across segments split by inline tags. Reset per chapter.
        advisory_blacklist: Optional list of context-dependent words to flag softly.

    Returns:
        (processed, has_blacklist, blacklist_matches) by default.
        If advisory_blacklist is provided, returns
        (processed, has_blacklist, blacklist_matches, advisory_matches).
    """
    # Phase A: Glossary replacement — skip English-heavy segments.
    # Short CJK glossary terms (e.g. "他"→"他") would match inside
    # English words (e.g. "he"→"他", corrupting "she"→"s他").
    if glossary:
        # Guard: if text is predominantly ASCII alpha, skip glo.
        # Without this, short CJK glossary terms like "他" would match
        # inside English words ("he"→"他", corrupting "she"→"s他").
        if _is_english_heavy(text, check_run=False):
            processed = text  # skip glossary on English/predominantly-ASCII text
        else:
            processed, _ = _GLOSSARY_REGEX.subn(
                lambda m: glossary.get(m.group(0), m.group(0)), text
            )
    else:
        processed = text

    # Phase A2: Semicolon replacement — Chinese fiction uses ； extremely rarely.
    # Most are English translation artifacts; replace with full-width comma.
    # Skip English/predominantly-ASCII text to avoid corrupting code/HTML.
    if not _is_english_heavy(processed, check_run=False):
        processed = processed.replace('\uff1b', '\uff0c')

    # Phase A3: ASCII straight quotes → Chinese curly quotes.
    # Alternating state machine: first " becomes \u201c, next becomes \u201d.
    # State persists across segments via quote_state to avoid same-direction
    # quotes when inline tags split a paragraph (Bug: <p>"Hello <em>World</em>"</p>
    # → 3 segments, each would reset left=True, corrupting paired quotes).
    if '"' in processed:
        result = []
        if quote_state is None:
            quote_state = {"left": True}
        for ch in processed:
            if ch == '"':
                result.append('\u201c' if quote_state["left"] else '\u201d')
                quote_state["left"] = not quote_state["left"]
            else:
                result.append(ch)
        processed = ''.join(result)

    # Phase B: Blacklist FLAGGING — mark for treatment, don't skip
    processed = apply_mechanical_style_fixes(processed)
    hits = [w for w in blacklist if w in processed]
    advisory_hits = [w for w in (advisory_blacklist or []) if w in processed]
    if advisory_blacklist is not None:
        return (processed, bool(hits), hits, advisory_hits)
    return (processed, bool(hits), hits)


# Module-level cache for glossary regex (built once per preprocess run)
_GLOSSARY_REGEX = re.compile('')


def _rebuild_glossary_regex(glossary):
    """Rebuild the module-level glossary regex cache."""
    global _GLOSSARY_REGEX
    _GLOSSARY_REGEX = _build_glossary_regex(glossary) or re.compile('')


def split_long_text(text, threshold=300, punctuations=None):
    """Split text by sentence boundaries if over threshold.

    Cutting occurs after sentence-ending punctuation (。？！),
    optionally followed by closing quotes/brackets. Without this,
    '他说："你好！"然后走了。' would split into ['他说："你好！',
    '"然后走了。'] — breaking quote pairing and causing LLM
    hallucinated quote fixes.

    Args:
        text: Text to potentially split
        threshold: Character count that triggers splitting
        punctuations: List of punctuation markers to split on

    Returns:
        list of sentence strings (always a list, length 1 if no split needed)
    """
    if punctuations is None:
        punctuations = ["。", "？", "！"]

    if len(text) <= threshold:
        return [text]

    # Closing symbols that should stay with the preceding sentence.
    # Without this, '他说："你好！"然后' splits into ['他说："你好！',
    # '"然后'] — orphan quote in each half, LLM hallucinates fixes.
    # Only unambiguous directional characters; ASCII " and ' are excluded
    # because they can be either opening or closing.
    closing_symbols = ['\u201d', '\u2019',   # right double/single smart quotes
                       '\u300d', '\u300f',   # 」』
                       '\uff09', '\u300b', '\u3011']  # ）》】
    escaped_punct = "".join(re.escape(p) for p in punctuations)
    escaped_close = "".join(re.escape(c) for c in closing_symbols)
    pattern = f"([^{escaped_punct}]*[{escaped_punct}][{escaped_close}]*)"
    parts = re.findall(pattern, text)

    # Catch trailing text without end punctuation.
    # Do NOT .strip() — leading/trailing whitespace in XHTML text nodes
    # is significant for inline-element spacing (e.g. <span>A</span> <span>B</span>).
    matched_len = sum(len(p) for p in parts)
    if matched_len < len(text):
        trailing = text[matched_len:]
        if trailing:
            parts.append(trailing)

    return parts if parts else [text]


def _detect_round3_patterns(text):
    """Detect mechanical Round 3 patterns for checklist hints.

    These are regex-level detections — hints for the LLM to evaluate,
    NOT auto-corrections. The LLM must still exercise judgment on each.

    Returns a dict with optional keys: 翻译腔, 数字, 长句, 粘滞.
    Empty dict if no patterns found.
    """
    result = {}

    # 1. Translationese patterns — all 7 patterns from SKILL.md Round 3 P1
    tl = []
    if re.search(r'被.{1,20}所', text):
        tl.append("被……所……")
    if re.search(r'开始.{1,30}起来', text):
        tl.append("开始……起来")
    if re.search(r'着[\u4e00-\u9fff]{1,20}着', text):
        tl.append("……着……着")
    # Pattern 4: 是……的 (at least 4 CJK chars between, to skip "是我的")
    if re.search(r'是[\u4e00-\u9fff]{4,30}的', text):
        tl.append("是……的")
    # Pattern 5: 一个……的 (adjectival clustering)
    if re.search(r'一个[\u4e00-\u9fff]{1,25}的', text):
        tl.append("一个……的")
    # Pattern 6: 过于正式的代词 (该/其 as standalone formal pronoun)
    # 该 followed by CJK char (not in compounds like 应该/活该)
    if re.search(r'(?<![应活])该[\u4e00-\u9fff]', text):
        tl.append("该(代词)")
    # 其 followed by CJK char (not in 其他/其它/其中/其实/其余/其次/尤其/及其)
    if re.search(r'(?<![尤与及])其(?!他|她|它|中|实|余|次)', text):
        tl.append("其(代词)")
    # Pattern 7: 被动语态过滥 — 被 + verb (≥2 occurrences, exclude fixed compounds)
    bei_hits = re.findall(r'被(?!迫|动|告|捕|害|控|诉|称|选举|认为)', text)
    bei_unique = set(bei_hits) if bei_hits else set()
    if len(bei_hits) >= 2:
        tl.append(f"被动语态({len(bei_hits)}处)")
    if tl:
        result["翻译腔"] = tl

    # 2. Arabic numerals (should be Chinese characters in literary text)
    nums = re.findall(r'\d+', text)
    if nums:
        result["数字"] = nums[:5]  # cap at 5 to avoid bloat

    # 3. Long sentences (>100 chars between sentence breaks)
    sentences = re.split(r'[。！？；\n]', text)
    max_len = max((len(s) for s in sentences), default=0)
    if max_len > 100:
        result["长句"] = max_len

    # 4. Consecutive same CJK chars (cross-word粘滞 candidates)
    cjk_pairs = re.findall(r'([\u4e00-\u9fff])\1', text)
    if cjk_pairs:
        result["粘滞"] = [c + c for c in cjk_pairs[:3]]  # cap at 3 pairs

    return result


def _dump_chapter_lines(chapter, data, clean_batches=False, round3=False):
    """Return (header_lines, content_lines, char_count, markers) for one chapter.

    Each segment gets its own coordinate line. No merging — merging
    would hide coordinates from the LLM, causing corrections to target
    the wrong segment and produce duplicated text on inject.

    When clean_batches=True, markers are separated into a dict (keyed by
    segment coordinate) instead of embedded in the text. This physically
    prevents the LLM from scanning markers to locate segments — it must
    read clean text first, then cross-reference the checklist file.
    """
    header = [
        f"\n{'='*60}",
        f"CHAPTER {chapter}: {data.get('file', '')}",
        f"{'='*60}\n",
    ]
    body = []
    markers = {}
    chars = 0

    for seg in data.get("segments", []):
        content = seg.get("content", "")
        if not content:
            continue
        coord = f"c{chapter}.s{seg.get('id', '?')}"
        sub_id = seg.get("sub_id")
        if sub_id is not None:
            coord += f".{sub_id}"

        seg_markers = {}

        if seg.get("blacklisted") and seg.get("blacklist_hits"):
            if clean_batches:
                seg_markers["网文词"] = seg["blacklist_hits"]
            else:
                bl_words = ", ".join(seg["blacklist_hits"])
                body.append(f"[? 需替换网文词: {bl_words}]")

        if seg.get("advisory_hits"):
            if clean_batches:
                seg_markers["审视词"] = seg["advisory_hits"]
            else:
                adv_words = ", ".join(seg["advisory_hits"])
                body.append(f"[? 建议审视用词: {adv_words}]")

        if seg.get("is_english"):
            # Check if nearby Chinese translation exists (bilingual → delete EN)
            all_segs = data.get("segments", [])
            seg_idx = next((k for k, s in enumerate(all_segs)
                           if s.get("id") == seg.get("id")
                           and s.get("sub_id") == seg.get("sub_id")), -1)
            has_cn = False
            if seg_idx >= 0:
                for j in range(max(0, seg_idx - 5), min(len(all_segs), seg_idx + 6)):
                    if j == seg_idx:
                        continue
                    n = all_segs[j].get("content", "")
                    cj = sum(1 for c in n if '\u4e00' <= c <= '\u9fff')
                    if cj < 20:
                        continue
                    if sum(1 for c in n if c.isascii() and c.isalpha()) / max(cj, 1) < 0.2:
                        has_cn = True
                        break
            if clean_batches:
                seg_markers["英文"] = "删除" if has_cn else "翻译"
            else:
                if has_cn:
                    body.append("[? 英文段落]")
                else:
                    body.append("[? 英文段落·待翻译]")

        if round3 and clean_batches:
            r3 = _detect_round3_patterns(content)
            if r3:
                seg_markers.update(r3)

        if clean_batches and seg_markers:
            markers[coord] = seg_markers

        body.append(f"[{coord}] {content}")
        chars += len(content)

    return header, body, chars, markers


def _iter_dump_chapters(index, extracted_dir, clean_batches=False, round3=False):
    """Yield (chapter, header, body_lines, char_count, markers) for each chapter.

    Prefers _corrected.json (LLM-proofread text) over _preprocessed.json
    (mechanical glossary + style fixes) over raw extract. This ensures
    round 2 sees round 1's corrections rather than re-reading stale text.

    markers is a dict keyed by segment coordinate (e.g. "c3.s15") →
    {"网文词": [...], "英文": "删除"|"翻译", "审视词": [...], "翻译腔": [...], ...}.
    Empty dict when clean_batches=False.
    """
    for item in index:
        chapter = item["chapter"]
        corr_path = extracted_dir / f"chapter_{chapter:04d}_corrected.json"
        corr_sentinel = extracted_dir / f"chapter_{chapter:04d}.corrected"
        pp_path = extracted_dir / f"chapter_{chapter:04d}_preprocessed.json"
        raw_path = extracted_dir / f"chapter_{chapter:04d}.json"

        if corr_path.exists() and corr_sentinel.exists():
            read_path = corr_path
        elif pp_path.exists():
            read_path = pp_path
        else:
            read_path = raw_path
        if not read_path.exists():
            continue
        with open(read_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        header, body, chars, markers = _dump_chapter_lines(chapter, data, clean_batches, round3)
        yield chapter, header, body, chars, markers


def _extract_tail(lines, max_chars):
    """Extract the last ~max_chars of text from batch lines for overlap."""
    tail = []
    remaining = max_chars
    for line in reversed(lines):
        # Skip header markers and coordinate-only lines
        if line.startswith("[") and line.endswith("]") and len(line) < 40:
            continue
        if line.startswith("=") or line.startswith("CHAPTER"):
            continue
        if len(line) <= remaining:
            tail.append(line)
            remaining -= len(line)
        else:
            tail.append(line[-remaining:])
            break
    return "\n".join(reversed(tail))


def _write_batch(batch_dir, num, lines, start_ch, end_ch, checklist=None):
    """Write a single batch file + optional checklist JSON."""
    fname = f"batch_{num:02d}_ch{start_ch:04d}_to_{end_ch:04d}.txt"
    with open(batch_dir / fname, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    if checklist:
        cname = f"batch_{num:02d}_ch{start_ch:04d}_to_{end_ch:04d}_checklist.json"
        with open(batch_dir / cname, "w", encoding="utf-8") as f:
            json.dump(checklist, f, ensure_ascii=False, indent=2)


def _redump_batches(work_dir, round3=False):
    """Regenerate batch files from current _preprocessed.json content.

    Called after reprocess updates the glossary so that batch files
    reflect the latest term replacements. Without this, subsequent
    proofreading rounds see stale text with old variant forms.

    Respects clean_batches from config — if pipeline was run with
    --clean-batches, redump also produces clean text + checklist files.

    When round3=True, also generates Round 3 mechanical markers
    (翻译腔 patterns, long sentences, numbers, CJK粘滞) and writes
    them to the checklist files — preventing empty-checklist tunnel-vision.
    """
    extracted_dir = Path(work_dir) / "extracted"
    batch_dir = Path(work_dir) / "proofread_batches"
    index_path = extracted_dir / "index.json"

    if not index_path.exists():
        return

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    # Read max_chars and clean_batches from config
    config = load_config(work_dir)
    max_chars = config.get("proofreading", {}).get("max_chars", 0) or 80000
    clean_batches = config.get("proofreading", {}).get("clean_batches", False)

    # Collect all chapters
    all_lines = []
    total_chars = 0
    chapters = []
    for ch, hdr, body, chars, markers in _iter_dump_chapters(index, extracted_dir, clean_batches, round3):
        chapters.append((ch, hdr, body, chars, markers))
        all_lines.extend(hdr)
        all_lines.extend(body)
        total_chars += chars

    # Rewrite full text
    out_path = Path(work_dir) / "full_text.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines))

    # Regenerate batches
    if batch_dir.exists():
        for f in batch_dir.glob("batch_*.txt"):
            f.unlink()
        for f in batch_dir.glob("batch_*_checklist.json"):
            f.unlink()
    batch_dir.mkdir(exist_ok=True)

    batch_num = 1
    batch_chars = 0
    batch_lines = []
    batch_markers = {} if clean_batches else None
    batch_start_ch = None
    batch_end_ch = None
    OVERLAP_CHARS = 2000
    prev_batch_tail = None

    for ch, hdr, body, chars, markers in chapters:
        if chars > max_chars:
            if batch_lines:
                _write_batch(batch_dir, batch_num, batch_lines, batch_start_ch, batch_end_ch,
                             batch_markers)
                prev_batch_tail = _extract_tail(batch_lines, OVERLAP_CHARS)
                batch_num += 1
                batch_lines, batch_chars = [], 0
                batch_markers = {} if clean_batches else None
                batch_start_ch = None
            chunk = []
            if prev_batch_tail:
                chunk.append(f"\n[Previous Context]\n{prev_batch_tail}\n[/Previous Context]\n")
            chunk.extend(hdr + body)
            _write_batch(batch_dir, batch_num, chunk, ch, ch,
                         markers if clean_batches else None)
            prev_batch_tail = _extract_tail(hdr + body, OVERLAP_CHARS)
            batch_num += 1
            continue

        if batch_chars + chars > max_chars and batch_lines:
            _write_batch(batch_dir, batch_num, batch_lines, batch_start_ch, batch_end_ch,
                         batch_markers)
            prev_batch_tail = _extract_tail(batch_lines, OVERLAP_CHARS)
            batch_num += 1
            batch_lines, batch_chars = [], 0
            batch_markers = {} if clean_batches else None
            batch_start_ch = None

        if batch_start_ch is None:
            batch_start_ch = ch
            if prev_batch_tail:
                batch_lines.append(
                    f"\n[Previous Context — 只读参考]\n{prev_batch_tail}\n"
                    f"[/Previous Context]\n"
                    f"[BOUNDARY CHECK — 对比此标记前后 3 段：语气、节奏、"
                    f"用词是否平滑衔接？若有风格突变，写入 corrections]\n"
                )
        batch_end_ch = ch
        batch_lines.extend(hdr)
        batch_lines.extend(body)
        if clean_batches and markers:
            batch_markers.update(markers)
        batch_chars += chars

    if batch_lines:
        _write_batch(batch_dir, batch_num, batch_lines, batch_start_ch, batch_end_ch,
                     batch_markers)

    print(f"  Batch files regenerated: {batch_num} batch(es) in {batch_dir}/")


def cmd_dump_text(args):
    """Dump all extracted text as a single file for LLM to read.

    Outputs a plain text file at work_dir/full_text.txt with chapter markers.
    The LLM reads this to do entity extraction, inconsistency detection,
    chunk boundary analysis, and full-book proofreading — all with semantic
    understanding that no Python heuristic can match.

    With --max-chars, auto-splits into batches when total exceeds the limit.
    With --clean-batches, markers ([? ...]) are stripped from batch text and
    written to separate *_checklist.json files — preventing LLM marker
    tunnel-vision by forcing active scanning before marker cross-reference.
    """
    work_dir = Path(args.work_dir)
    extracted_dir = work_dir / "extracted"
    clean_batches = getattr(args, 'clean_batches', False)

    index_path = extracted_dir / "index.json"
    if not index_path.exists():
        print("  No index.json found. Run extract first.")
        return 1

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    # Collect all chapter data (single pass)
    all_lines = []
    total_chars = 0
    chapters = []  # (chapter, header, body, chars, markers)
    for ch, hdr, body, chars, markers in _iter_dump_chapters(index, extracted_dir, clean_batches):
        chapters.append((ch, hdr, body, chars, markers))
        all_lines.extend(hdr)
        all_lines.extend(body)
        total_chars += chars

    # Always write full text
    out_path = work_dir / "full_text.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines))
    print(f"  Full text: {total_chars} chars → {out_path}")

    # Auto-split into batches if --max-chars is set and total exceeds it.
    max_chars = getattr(args, 'max_chars', 0) or 0
    if max_chars > 0 and total_chars > max_chars:
        batch_dir = work_dir / "proofread_batches"
        # Clean stale batch files from previous run (different --max-chars
        # or deleted chapters could leave orphaned batches behind).
        if batch_dir.exists():
            for f in batch_dir.glob("batch_*.txt"):
                f.unlink()
            for f in batch_dir.glob("batch_*_checklist.json"):
                f.unlink()
        batch_dir.mkdir(exist_ok=True)
        batch_num = 1
        batch_chars = 0
        batch_lines = []
        batch_markers = {} if clean_batches else None
        batch_start_ch = None
        batch_end_ch = None

        OVERLAP_CHARS = 2000
        prev_batch_tail = None  # last N chars of previous batch for overlap

        for ch, hdr, body, chars, markers in chapters:
            if chars > max_chars:
                if batch_lines:
                    _write_batch(batch_dir, batch_num, batch_lines, batch_start_ch, batch_end_ch,
                                 batch_markers)
                    prev_batch_tail = _extract_tail(batch_lines, OVERLAP_CHARS)
                    print(f"  Batch {batch_num:02d}: ch {batch_start_ch:04d}-{batch_end_ch:04d} ({batch_chars} chars)")
                    batch_num += 1
                    batch_lines, batch_chars = [], 0
                    batch_markers = {} if clean_batches else None
                    batch_start_ch = None
                chunk = []
                if prev_batch_tail:
                    chunk.append(
                    f"\n[Previous Context — 只读参考]\n{prev_batch_tail}\n"
                    f"[/Previous Context]\n"
                    f"[BOUNDARY CHECK — 对比此标记前后 3 段：语气、节奏、"
                    f"用词是否平滑衔接？若有风格突变，写入 corrections]\n"
                )
                chunk.extend(hdr + body)
                _write_batch(batch_dir, batch_num, chunk, ch, ch,
                             markers if clean_batches else None)
                prev_batch_tail = _extract_tail(hdr + body, OVERLAP_CHARS)
                print(f"  Batch {batch_num:02d}: ch {ch:04d} alone ({chars} chars)")
                batch_num += 1
                continue

            if batch_chars + chars > max_chars and batch_lines:
                _write_batch(batch_dir, batch_num, batch_lines, batch_start_ch, batch_end_ch,
                             batch_markers)
                prev_batch_tail = _extract_tail(batch_lines, OVERLAP_CHARS)
                print(f"  Batch {batch_num:02d}: ch {batch_start_ch:04d}-{batch_end_ch:04d} ({batch_chars} chars)")
                batch_num += 1
                batch_lines, batch_chars = [], 0
                batch_markers = {} if clean_batches else None
                batch_start_ch = None

            if batch_start_ch is None:
                batch_start_ch = ch
                # Prepend overlap from previous batch as read-only context
                if prev_batch_tail:
                    batch_lines.append(
                        f"\n[Previous Context — 只读参考]\n{prev_batch_tail}"
                        f"\n[/Previous Context]\n"
                        f"[BOUNDARY CHECK — 对比此标记前后 3 段]\n"
                    )
            batch_end_ch = ch
            batch_lines.extend(hdr)
            batch_lines.extend(body)
            if clean_batches and markers:
                batch_markers.update(markers)
            batch_chars += chars

        if batch_lines:
            _write_batch(batch_dir, batch_num, batch_lines, batch_start_ch, batch_end_ch,
                         batch_markers)
            print(f"  Batch {batch_num:02d}: ch {batch_start_ch:04d}-{batch_end_ch:04d} ({batch_chars} chars)")

        print(f"  Split into {batch_num} batch(es) in {batch_dir}/")
        if clean_batches:
            print(f"  Clean mode: markers in *_checklist.json, text files are marker-free.")
        print(f"  LLM: Process batches sequentially. Each batch fits in context.")
    else:
        print(f"  LLM: Read this file to do entity extraction, inconsistency detection,")
        print(f"       chunk boundary analysis, and full-book proofreading.")
    return 0


def cmd_apply_corrections(args):
    """Apply structured LLM corrections to the proofread output.

    Reads a JSON file with Claude's output:
    {
      "glossary_additions": [{"term": "亚拉冈", "translation": "阿拉贡"}, ...],
      "corrections": [{"chapter": 0, "segment_id": 3, "corrected": "修正后的文本"}, ...]
    }

    Automatically:
      1. Adds all glossary_additions terms
      2. Applies corrections to the specified segments in _preprocessed.json
      3. Runs reprocess to mechanically apply the new glossary
    """
    work_dir = Path(args.work_dir)
    corrections_file = args.corrections_json

    # Read Claude's structured output
    if corrections_file == "-":
        # Read from stdin
        raw = sys.stdin.read()
    else:
        with open(corrections_file, "r", encoding="utf-8") as f:
            raw = f.read()

    # Extract ALL JSON blocks (LLM may split glossary_additions and
    # corrections into separate ```json fences). Merge them.
    json_blocks = re.findall(r"```(?:json)?\s*\n?(.*?)```", raw, re.DOTALL)
    data = {}
    parse_errors = 0
    if json_blocks:
        for block in json_blocks:
            # LLMs frequently produce malformed JSON. Apply common fixes:
            # 1. Trailing commas (most common LLM JSON error)
            block = re.sub(r',\s*([}\]])', r'\1', block)
            # 2. JavaScript comments: // ... and /* ... */
            block = re.sub(r'//[^\n]*', '', block)
            block = re.sub(r'/\*.*?\*/', '', block, flags=re.DOTALL)
            # 3. Unquoted keys: {key: → {"key": (but not inside strings)
            block = re.sub(r'\{(\s*)([a-zA-Z_]\w*)\s*:', r'{\1"\2":', block)
            block = re.sub(r',(\s*)([a-zA-Z_]\w*)\s*:', r',\1"\2":', block)
            try:
                block_data = json.loads(block, strict=False)
                for k, v in block_data.items():
                    if k in data and isinstance(data[k], list) and isinstance(v, list):
                        data[k].extend(v)
                    else:
                        data[k] = v
            except json.JSONDecodeError as e:
                parse_errors += 1
                print(f"  Warning: JSON parse error in block: {e}")
                print(f"  Block preview: {block[:200]}...")
        if parse_errors:
            print(f"  ({parse_errors} block(s) failed to parse, skipped)")
    else:
        # No code fences — try the raw text directly
        raw_clean = re.sub(r',\s*([}\]])', r'\1', raw)
        try:
            data = json.loads(raw_clean, strict=False)
        except json.JSONDecodeError as e:
            print(f"  Error parsing Claude output: {e}")
            return 1

    # ═══ CORRECT ORDER: corrections first, then reprocess ═══
    # LLM may write "亚拉冈" in its corrected text because it didn't yet
    # know about the new glossary term. Writing corrections FIRST, then
    # running reprocess, lets the glossary regex scrub LLM's text too.
    # cmd_reprocess already updates _corrected.json content in-place.

    # 1. Add glossary terms
    glossary_additions = data.get("glossary_additions", [])
    if glossary_additions:
        added, updated, unchanged, chain_resolved = add_terms_batch(glossary_additions, work_dir)
        print(f"  Glossary: {added} added, {updated} updated, {unchanged} unchanged")

    # 2. Apply corrections FIRST — write LLM text to _corrected.json
    corrections = data.get("corrections", [])
    extracted_dir = work_dir / "extracted"
    if corrections:
        by_chapter = {}
        for c in corrections:
            if "chapter" not in c:
                print(f"  Warning: skipping correction without 'chapter' field: {c.get('segment_id', '?')}")
                continue
            try:
                ch = int(c["chapter"])
            except (ValueError, TypeError):
                continue
            by_chapter.setdefault(ch, []).append(c)

        applied = 0
        for ch, corrs in by_chapter.items():
            ch_path = extracted_dir / f"chapter_{ch:04d}.json"
            pp_path = extracted_dir / f"chapter_{ch:04d}_preprocessed.json"
            corr_path = extracted_dir / f"chapter_{ch:04d}_corrected.json"
            sentinel = extracted_dir / f"chapter_{ch:04d}.corrected"

            if sentinel.exists() and corr_path.exists():
                base_path = corr_path  # accumulate on previous corrections
            else:
                base_path = pp_path if pp_path.exists() else ch_path  # first call
            if not base_path.exists():
                print(f"  Warning: chapter {ch} file not found ({base_path.name})")
                continue

            with open(base_path, "r", encoding="utf-8") as f:
                ch_data = json.load(f)

            seg_map = {}
            has_subs = any("sub_id" in s for s in ch_data.get("segments", []))
            if has_subs:
                grouped = {}
                for seg in ch_data.get("segments", []):
                    grouped.setdefault(str(seg["id"]), []).append(seg)
                for sid, parts in grouped.items():
                    for s in parts:
                        seg_map[f"{s['id']}.{s['sub_id']}"] = s
                    full_text = "".join(p.get("content", "") for p in parts)
                    seg_map[sid] = {"content": full_text, "_parts": parts}
            else:
                for seg in ch_data.get("segments", []):
                    seg_map[str(seg["id"])] = seg

            for corr in corrs:
                raw_sid = corr.get("segment_id", "")
                # When data has sub-segments, keep "N.0" as "N.0" to match
                # individual sub-segment key (not the merged _parts entry).
                sid = _normalize_segment_id(raw_sid, keep_dot_zero=has_subs)
                corrected = corr.get("corrected")
                # Empty string is a valid correction (deletion of segment content).
                # Use `corr.get("corrected") is not None` to distinguish "delete"
                # from "no correction provided for this segment_id".
                if sid in seg_map and corrected is not None:
                    target = seg_map[sid]
                    if "_parts" in target:
                        parts = target["_parts"]
                        if parts:
                            parts[0]["content"] = corrected
                            for p in parts[1:]:
                                p["content"] = ""
                        applied += 1
                    else:
                        target["content"] = corrected
                        applied += 1

            out_path = extracted_dir / f"chapter_{ch:04d}_corrected.json"
            sentinel_path = extracted_dir / f"chapter_{ch:04d}.corrected"
            # Touch sentinel BEFORE atomic data write. If crash occurs after
            # sentinel touch, the sentinel exists and the old _corrected.json
            # is still intact (atomic replace hasn't happened yet). Next run
            # will use the old version and re-apply corrections on top.
            # If crash occurs after atomic replace, both sentinel and data
            # are consistent.
            sentinel_path.touch()
            tmp_path = extracted_dir / f"chapter_{ch:04d}_corrected.json.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(ch_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, out_path)

        print(f"  Corrections: {applied} applied to chapter_NNNN_corrected.json")

        # Post-apply verification: detect front-loaded corrections
        # (LLM only processed early segments, skipped the rest of the chapter)
        if corrections:
            for ch, corrs in by_chapter.items():
                ch_path = (extracted_dir / f"chapter_{ch:04d}_preprocessed.json")
                if not ch_path.exists():
                    ch_path = extracted_dir / f"chapter_{ch:04d}.json"
                if not ch_path.exists():
                    continue
                with open(ch_path, "r", encoding="utf-8") as f:
                    ch_data = json.load(f)
                total_segs = len(ch_data.get("segments", []))
                if total_segs < 30:
                    continue
                max_sid = 0
                for c in corrs:
                    try:
                        sid = int(str(c.get("segment_id", "0")).split(".")[0])
                        max_sid = max(max_sid, sid)
                    except (ValueError, IndexError):
                        pass
                if max_sid > 0 and max_sid / total_segs < 0.20:
                    print(f"  ⚠  Chapter {ch}: last corrected segment #{max_sid}/{total_segs} "
                          f"({max_sid/total_segs:.0%}) — all corrections in early segments, "
                          f"possible lazy scanning")

    # 3. Selective reprocess AFTER corrections — only chapters containing
    #    new glossary terms. Avoids full rescan for large books where new
    #    terms appear in few chapters.
    if glossary_additions:
        new_terms = set()
        for entry in glossary_additions:
            term = entry.get("term", "").strip()
            if term:
                new_terms.add(term)
        if new_terms:
            print(f"  Reprocessing with updated glossary ({len(new_terms)} new terms)...")
            fake_args = argparse.Namespace(work_dir=str(work_dir))
            cmd_reprocess(fake_args, new_terms=new_terms)

    # No sentinel cleanup needed: cmd_reprocess already updates
    # _corrected.json content with new glossary terms for ALL chapters
    # (including those not re-corrected in this batch). Sentinels
    # persist to ensure future check/inject continue using the
    # LLM-corrected text (now with updated glossary).

    # cmd_inject and cmd_check now prefer _corrected.json, falling back to
    # _preprocessed.json and then chapter_NNNN.json. No mirroring needed —
    # raw extract files remain intact as the single source of truth for
    # cmd_reprocess.

    # 4. Report conflicts
    conflicts = data.get("conflicts_need_human", [])
    if conflicts:
        print(f"\n  === Conflicts needing human review ({len(conflicts)}) ===")
        for c in conflicts:
            print(f"    {' vs '.join(c.get('variants', []))}: {c.get('reason', '')}")

    # 5. Report boundary issues
    boundary = data.get("boundary_issues", [])
    if boundary:
        print(f"\n  === AI chunk boundary issues ({len(boundary)}) ===")
        for b in boundary:
            print(f"    ch{b.get('chapter', '?')}: {b.get('detail', '')}")

    return 0


def compute_change_ratio(original, proofread_text):
    """Compute edit distance ratio between original and proofread text.

    Uses difflib.SequenceMatcher for accurate sequence comparison
    (not character-set overlap, which would pass e.g. "abc"→"cba" as 0% change).

    Returns a ratio from 0.0 (identical) to 1.0 (completely different).
    Used to enforce the ≤40% change limit per segment.
    """
    if not original or original == proofread_text:
        return 0.0
    from difflib import SequenceMatcher
    return 1.0 - SequenceMatcher(None, original, proofread_text).ratio()


def _has_doubled_cjk(text):
    """Return True if text contains consecutive repeated CJK chars."""
    return bool(re.search(r'([\u4e00-\u9fff])\1', text))


def _validate_glossary_entry(term, translation):
    """Return None when safe, otherwise a rejection reason."""
    if not term or not translation:
        return "empty term or translation"
    if re.fullmatch(r'[\u4e00-\u9fff]', term):
        return "single-character CJK key is unsafe"
    if _has_doubled_cjk(translation):
        return "target has doubled CJK characters"
    return None


def _count_glossary_residual(html_text, term, target):
    """Count source term residuals, ignoring occurrences inside canonical target."""
    scan_text = html_text
    if target and target != term:
        scan_text = scan_text.replace(target, "")
    return scan_text.count(term)


def add_term_to_glossary(term, translation, work_dir):
    """Add a new term→translation pair to the glossary.

    Args:
        term: Original term (Chinese or foreign)
        translation: Unified translation
        work_dir: Path to work directory containing glossary.json

    Returns:
        True if term was newly added, False if already exists
    """
    glossary = load_glossary(work_dir)

    reason = _validate_glossary_entry(term, translation)
    if reason:
        print(f"  WARNING: rejected glossary entry {term} -> {translation}: {reason}")
        return False

    if term in glossary:
        if glossary[term] == translation:
            return False  # already exists with same value
        # Update existing term
        glossary[term] = translation
        save_glossary(glossary, work_dir)
        print(f"  Glossary updated: {term} -> {translation}")
        return True
    glossary[term] = translation
    save_glossary(glossary, work_dir)
    print(f"  Glossary added: {term} -> {translation}")
    return True


# ---------------------------------------------------------------------------
# Proofreading rule helpers (mechanical checks Claude cannot do alone)
# ---------------------------------------------------------------------------

def mechanical_proofreading_checks(original, proofread, config):
    """Run all mechanical checks on a proofread segment.

    Returns: (passed, issues) where passed is bool and issues is list of strings
    """
    max_change = config.get("proofreading", {}).get("max_change_ratio", 0.4)
    blacklist = config.get("blacklist", [])
    issues = []

    # Check 1: Change ratio
    ratio = compute_change_ratio(original, proofread)
    if ratio > max_change:
        issues.append(f"Change ratio {ratio:.2f} exceeds max {max_change}")

    # Check 2: Blacklist in result (should never happen, but safety check)
    for word in blacklist:
        if word in proofread and word not in original:
            issues.append(f"New blacklist word introduced: {word}")

    # Check 3: Doubled CJK characters (LLM output artifact).
    # Consecutive repeats of 3+ identical CJK characters (e.g. 特特特, 尔尔尔)
    # are almost certainly generation artifacts. Legitimate triples are
    # vanishingly rare in Chinese prose.
    doubled = re.findall(r'([\u4e00-\u9fff])\1{2,}', proofread)
    if doubled:
        unique = set(doubled)
        issues.append(f"Doubled CJK chars (generation artifact): {', '.join(c*3 for c in unique)}")

    return len(issues) == 0, issues


def apply_mechanical_style_fixes(text):
    """Apply deterministic, safe style fixes to text before Claude proofreads.

    These are mechanical transformations that don't require judgment:
    - English punctuation → Chinese punctuation
    - Unambiguous translation patterns
    - Whitespace normalization

    Returns: fixed_text
    """
    # Strip EPUB template artifacts like {{id_3792}} or {id_3792}
    text = re.sub(r'\{\{[a-z_]+\d*\}\}', '', text)
    text = re.sub(r'\{[a-z_]+\d*\}', '', text)

    # --- 标点规范化 (deterministic, no judgment needed) ---

    # English double/single quotes → Chinese quotes is NOT done here.
    # Quote pairing requires cross-segment context (quotes often span
    # multiple HTML nodes). This is left to LLM phase C proofreading.

    # Ellipsis: three dots → proper ellipsis
    text = text.replace('...', '\u2026\u2026')

    # Em dash: double hyphen → em dash
    text = text.replace('--', '\u2014\u2014')

    # English comma/semicolon/colon → Chinese
    # Must NOT convert commas inside numbers (123,456) or URLs.
    # Use negative lookahead instead of positive — the latter fails on
    # text-node-final punctuation (e.g. <p>他说,</p> has no char after ,).
    text = re.sub(r'(?<=[\u4e00-\u9fff\s]),(?![a-zA-Z0-9])', '\uff0c', text)
    # Semicolon: CJK-context → fullwidth comma (not fullwidth semicolon).
    # Chinese fiction uses ； extremely rarely; most are translation artifacts.
    # Must target ，not ；— otherwise Phase A2 (which runs before this function
    # in proofread_text) never sees ASCII semicolons converted here, and the
    # ；→， pass in proofread_text misses them.
    text = re.sub(r'(?<=[\u4e00-\u9fff]);(?![a-zA-Z0-9])', '\uff0c', text)
    # Catch any ； that survived Phase A2 (e.g. from reprocess or edge cases)
    text = text.replace('\uff1b', '\uff0c')
    text = re.sub(r'(?<=[\u4e00-\u9fff]):(?![a-zA-Z0-9])', '\uff1a', text)
    # --- 翻译腔修正 is NOT done mechanically ---
    # "被X所Y", "开始X起来" and similar patterns cannot be blindly
    # regex-replaced — they match common Chinese vocabulary (被子, 被告,
    # 被害人, etc.). Translation腔 correction is left entirely to LLM phase C.
    text = re.sub(r'[ \t]+([，。？！；：])', r'\1', text)  # no space before Chinese punct
    text = re.sub(r'([，。？！；：])[ \t]+', r'\1', text)   # no space after Chinese punct

    return text



def add_terms_batch(terms_list, work_dir):
    """Add multiple terms to glossary at once.

    Automatically resolves glossary chains (A→B, B→C): if a new term's
    translation is itself a glossary key that maps elsewhere, the term is
    remapped directly to the chain's final value. This is necessary because
    re.subn is single-pass — without it, A→B happens but B→C never follows,
    leaving intermediate forms "stuck" in the text.

    Args:
        terms_list: list of {"term": str, "translation": str} dicts
        work_dir: path to work directory

    Returns:
        (added, updated, unchanged, chain_resolved) counts
    """
    glossary = load_glossary(work_dir)
    added = updated = unchanged = 0
    chain_resolved = 0

    rejected = 0
    for entry in terms_list:
        term = entry.get("term", "").strip()
        trans = entry.get("translation", "").strip()
        if not term or not trans:
            continue

        reason = _validate_glossary_entry(term, trans)
        if reason:
            print(f"  WARNING: rejected glossary_additions entry {term} -> {trans}: {reason}")
            rejected += 1
            continue

        # Resolve chains: if trans is itself a key, follow to final value
        original_trans = trans
        seen = {trans}
        while trans in glossary and glossary[trans] != trans:
            trans = glossary[trans]
            if trans in seen:
                break  # circular reference, stop
            seen.add(trans)
        if trans != original_trans:
            chain_resolved += 1

        if term in glossary:
            if glossary[term] == trans:
                unchanged += 1
            else:
                glossary[term] = trans
                updated += 1
        else:
            glossary[term] = trans
            added += 1

    # After all terms are added, do a full pass to resolve all chains.
    # This also fixes existing entries that were added before their
    # translation became a key (e.g. A->B was added, then B->C was added
    # later — A should now map to C directly).
    retro_fixed = 0
    for k, v in glossary.items():
        original_v = v
        seen = {v}
        while v in glossary and glossary[v] != v:
            v = glossary[v]
            if v in seen:
                break  # circular reference, stop
            seen.add(v)
        if v != original_v:
            glossary[k] = v
            retro_fixed += 1

    save_glossary(glossary, work_dir)
    total_chain = chain_resolved + retro_fixed
    if total_chain:
        print(f"  Chains resolved: {total_chain} entries remapped to final canonical form "
              f"({chain_resolved} new, {retro_fixed} retroactive)")
    return added, updated, unchanged, chain_resolved + retro_fixed


# ---------------------------------------------------------------------------
# Glossary & config management
# ---------------------------------------------------------------------------

def load_glossary(work_dir):
    """Load glossary from work directory."""
    path = Path(work_dir) / GLOSSARY_FILENAME
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_glossary(glossary, work_dir):
    """Save glossary to work directory.

    Before saving, auto-generates identity mappings for glossary values
    that contain expandable glossary keys as substrings. This prevents the
    "substring expansion" bug where "菲德"→"菲德蕾" causes the existing
    word "菲德蕾" to be split into "菲德"+"蕾"→"菲德蕾蕾".

    Only CJK values (no ASCII letters) are protected, since ASCII-key
    glossary entries handle English→Chinese and don't affect existing
    Chinese text.
    """
    # Find expandable keys: CJK keys whose translation is longer than the key
    expandable = {}
    for k, v in glossary.items():
        if len(v) > len(k) and not any(c.isascii() and c.isalpha() for c in k):
            expandable[k] = v

    if expandable:
        # For each glossary value, check if any expandable key is a substring.
        # If so, ensure the value has an identity mapping to protect it.
        added = 0
        for v in list(glossary.values()):
            if any(c.isascii() and c.isalpha() for c in v):
                continue  # skip values with ASCII (they're English→Chinese targets)
            if v in glossary and glossary[v] == v:
                continue  # already has identity mapping
            for k in expandable:
                if k in v and k != v:
                    if v not in glossary:
                        glossary[v] = v  # identity mapping
                        added += 1
                    # break inner for-k-in-expandable loop; one identity
                    # mapping per value is enough. Next outer v continues.
                    break

    path = Path(work_dir) / GLOSSARY_FILENAME
    # Atomic write: dump to temp file then replace.
    # Prevents truncated glossary on crash or disk-full.
    tmp_path = Path(work_dir) / (GLOSSARY_FILENAME + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(glossary, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def copy_glossary(src, work_dir):
    """Copy external glossary into work directory."""
    if src and os.path.exists(src):
        shutil.copy2(src, Path(work_dir) / GLOSSARY_FILENAME)


def load_config(work_dir):
    """Load config from work directory; fall back to default."""
    config_path = Path(work_dir) / "config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    # Fall back to default
    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"blacklist": [], "proofreading": {}}


def save_config(config, work_dir):
    """Save config to work directory."""
    path = Path(work_dir) / "config.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _is_safe_epub_member(name):
    """Return True if a ZIP member path cannot escape the extraction dir."""
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    return not (normalized.startswith("/") or ".." in parts or ":" in normalized)


def _safe_extract_epub(zf, work_dir):
    """Extract EPUB entries without allowing archive paths outside work_dir."""
    for info in zf.infolist():
        if not _is_safe_epub_member(info.filename):
            raise ValueError(f"Unsafe EPUB entry path: {info.filename}")
        zf.extract(info, work_dir)


def _read_xhtml_text(filepath):
    """Read XHTML/HTML bytes using the same encoding detection as parsing."""
    with open(filepath, "rb") as f:
        return _decode_xhtml(f.read())


# ---------------------------------------------------------------------------
# Core commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Initialize work directory from EPUB.

    Project directory = {novel_name}/  (derived from EPUB filename)
    Work directory = {project_dir}/work/  (temporary, cleaned after pack)

    Persistent files (glossary, config, failed_list) live in project directory.
    """
    input_path = Path(args.input_epub).resolve()
    if not input_path.exists():
        print(f"  Error: {input_path} not found!")
        return 1

    # Novel name from EPUB filename (without .epub extension)
    novel_name = input_path.stem

    # Project directory: proofread/{novel_name}/ by default, or the explicit
    # work directory's parent when --work-dir is supplied.
    if args.work_dir:
        work_dir = Path(args.work_dir)
        project_dir = work_dir.parent
    else:
        project_dir = Path.cwd() / "proofread" / novel_name
        work_dir = project_dir / "work"
    project_dir.mkdir(parents=True, exist_ok=True)

    if work_dir.exists():
        # Safety guard: refuse to delete directories that don't look like
        # our work dir (prevents accidental data loss from --work-dir typos).
        contents = list(work_dir.iterdir())
        if contents and not (work_dir / "context.json").exists():
            print(f"  [Error] Safety guard: {work_dir} contains unknown files "
                  f"and is not a recognized work directory.")
            print(f"  Refusing to delete. Check your --work-dir path.")
            return 1
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    # Unzip EPUB
    try:
        with zipfile.ZipFile(str(input_path), "r") as zf:
            _safe_extract_epub(zf, work_dir)
    except ValueError as e:
        print(f"  Error: {e}")
        return 1
    print(f"  EPUB: {input_path.name}")

    # --- Glossary ---
    # Priority: 1) --glossary flag  2) existing project glossary  3) empty
    if args.glossary:
        copy_glossary(args.glossary, work_dir)
        print(f"  Glossary loaded: {args.glossary}")
    else:
        existing_glossary = project_dir / GLOSSARY_FILENAME
        if existing_glossary.exists():
            shutil.copy2(existing_glossary, work_dir / GLOSSARY_FILENAME)
            print(f"  Glossary loaded from project ({len(json.load(open(existing_glossary, encoding='utf-8')))} terms)")
        else:
            save_glossary({}, work_dir)
            print("  Empty glossary")

    # --- Blacklist ---
    blacklist, advisory_blacklist = load_blacklist_terms(
        profile=getattr(args, 'profile', 'fantasy'),
        custom_file=getattr(args, 'blacklist_file', None)
    )
    print(f"  Blacklist: {len(blacklist)} hard + {len(advisory_blacklist)} advisory words "
          f"(profile={getattr(args, 'profile', 'fantasy')})")

    # --- Config ---
    build_config(work_dir, project_dir, blacklist, advisory_blacklist, args)

    # Save context for later commands
    context = {
        "project_dir": str(project_dir.resolve()),
        "novel_name": novel_name,
        "input_epub": str(input_path)
    }
    with open(work_dir / "context.json", "w", encoding="utf-8") as f:
        json.dump(context, f, ensure_ascii=False, indent=2)

    print(f"  Project: {project_dir}/")
    print(f"  Work:    {work_dir}/")
    return 0


def build_config(work_dir, project_dir, blacklist, advisory_blacklist, args):
    """Build merged config: default → shipped overrides → project overrides → --config flag."""
    config = {}
    # Layer 1: default
    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)

    # Layer 2: project config (if exists)
    project_config = project_dir / CONFIG_FILENAME
    if project_config.exists():
        with open(project_config, "r", encoding="utf-8") as f:
            config.update(json.load(f))

    # Layer 3: --config flag (highest priority)
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            config.update(json.load(f))

    # Always override blacklist with the one we loaded
    config["blacklist"] = blacklist
    config["blacklist_advisory"] = advisory_blacklist

    save_config(config, work_dir)

    # Save to project dir WITHOUT --config overrides.
    # --config is a one-time flag; its settings should NOT leak into
    # the persistent project config and affect future runs.
    project_config_out = {}
    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            project_config_out = json.load(f)
    if project_config.exists():
        with open(project_config, "r", encoding="utf-8") as f:
            project_config_out.update(json.load(f))
    # Preserve project's own blacklist or use the loaded one
    if project_config.exists():
        with open(project_config, "r", encoding="utf-8") as f:
            existing = json.load(f)
            project_config_out["blacklist"] = existing.get("blacklist", blacklist)
            project_config_out["blacklist_advisory"] = existing.get("blacklist_advisory", advisory_blacklist)
    else:
        project_config_out["blacklist"] = blacklist
        project_config_out["blacklist_advisory"] = advisory_blacklist
    save_config(project_config_out, project_dir)


def cmd_extract(args):
    """Extract text segments from all XHTML files."""
    work_dir = Path(args.work_dir)
    extracted_dir = work_dir / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)

    # Read spine
    spine = read_spine(work_dir)
    xhtml_files = get_xhtml_files(work_dir, spine)

    if not xhtml_files:
        print("  No XHTML/HTML files found!")
        return 1

    index = []
    wd_resolved = work_dir.resolve()
    for i, fp in enumerate(xhtml_files):
        try:
            tree, root, etree_mod = parse_xhtml(fp)
            segments = extract_text_segments(root)

            # Resolve both to absolute for relative_to on Windows.
            # Must normalize to POSIX slashes — the path is stored in JSON
            # and may be consumed on a different OS (e.g. extract on Windows,
            # inject on Linux). Backslashes would be treated as part of the
            # filename and break all downstream commands.
            try:
                rel_path = str(fp.resolve().relative_to(wd_resolved)).replace('\\', '/')
            except ValueError:
                rel_path = os.path.relpath(str(fp), str(wd_resolved)).replace('\\', '/')

            out = {
                "chapter": i,
                "file": rel_path,
                "segments": segments
            }

            out_path = extracted_dir / f"chapter_{i:04d}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)

            # Also create a plain text version for easy reading
            text_only = "\n".join(s["content"] for s in segments)
            txt_path = extracted_dir / f"chapter_{i:04d}.txt"
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text_only)

            index.append({
                "chapter": i,
                "file": rel_path,
                "segments": len(segments),
                "chars": sum(len(s["content"]) for s in segments)
            })
            print(f"  [{i:04d}] {rel_path} — {len(segments)} segments")
        except Exception as e:
            print(f"  [{i:04d}] ERROR: {fp.name} — {e}")
            # Add to failed list
            with open(work_dir / FAILED_LIST_FILENAME, "a", encoding="utf-8") as f:
                f.write(f"{fp}\n")

    # Write index
    index_path = extracted_dir / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    total_segments = sum(item["segments"] for item in index)
    total_chars = sum(item["chars"] for item in index)
    print(f"\n  Total: {len(index)} chapters, {total_segments} segments, {total_chars} chars")
    return 0


def cmd_preprocess(args):
    """Apply mechanical proofreading (phases A+B) to extracted text.

    For each chapter JSON:
      - Applies glossary replacement (phase A)
      - Checks blacklist (phase B) — blacklisted segments revert to original
      - Splits long segments (>threshold chars) into sentences
      - Saves _preprocessed.json with blacklist status preserved

    This is a mechanical pass. Claude reads the preprocessed output and
    applies phase C (linguistic proofreading) afterward.
    """
    work_dir = Path(args.work_dir)
    extracted_dir = work_dir / "extracted"

    index_path = extracted_dir / "index.json"
    if not index_path.exists():
        print("  No index.json found. Run extract first.")
        return 1

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    glossary = load_glossary(work_dir)
    # Build regex once for the entire preprocess run (O(n) per segment)
    _rebuild_glossary_regex(glossary)
    config = load_config(work_dir)
    blacklist = config.get("blacklist", [])
    advisory_blacklist = config.get("blacklist_advisory", config.get("advisory", []))
    threshold = config.get("proofreading", {}).get("long_text_threshold", 300)
    split_punc = config.get("proofreading", {}).get("split_punctuation", ["。", "？", "！"])

    total_blacklisted = 0
    total_sentences = 0

    for item in index:
        chapter = item["chapter"]

        chapter_path = extracted_dir / f"chapter_{chapter:04d}.json"
        if not chapter_path.exists():
            continue

        with open(chapter_path, "r", encoding="utf-8") as f:
            chapter_data = json.load(f)

        # Apply proofread_text (phases A+B) and sentence splitting
        proofread_segments = []
        quote_state = {"left": True}  # persist quote parity across segments
        for seg in chapter_data.get("segments", []):
            original = seg.get("content", "")
            processed, blacklisted, bl_hits, advisory_hits = proofread_text(
                original, glossary, blacklist, quote_state, advisory_blacklist
            )
            if blacklisted:
                total_blacklisted += 1
            # Blacklisted segments still get full treatment — the flag only
            # marks words that Claude should replace during phase C
            # Flag English-heavy segments so ALL sub-segments get the marker
            is_english = _is_english_heavy(processed)
            sentences = split_long_text(processed, threshold, split_punc)
            total_sentences += len(sentences)

            # Write back as sub-segments for fine-grained proofreading
            for j, sentence in enumerate(sentences):
                proofread_segments.append({
                    "id": seg["id"],
                    "sub_id": j,
                    "type": seg.get("type", "text"),
                    "original": original if j == 0 else "",
                    "content": sentence,
                    "blacklisted": blacklisted,
                    "blacklist_hits": bl_hits,
                    "advisory_hits": advisory_hits,
                    "is_english": is_english,
                })

        # Save preprocessed version
        preproc_path = extracted_dir / f"chapter_{chapter:04d}_preprocessed.json"
        out = {
            "chapter": chapter,
            "file": chapter_data.get("file", ""),
            "segments": proofread_segments,
            "_note": "Preprocessed (A: glossary, B: blacklist-flagged, style-fixes). "
                      "blacklisted=true means segment contains网文词汇 needing replacement. "
                      "Claude: ALL segments get phase C proofreading. For blacklisted ones, "
                      "replace the flagged words with neutral alternatives."
        }
        with open(preproc_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

        # Record blacklisted chapters to failed_list
        bl_in_chapter = [s for s in proofread_segments if s.get("blacklisted")]
        if bl_in_chapter:
            bl_words = set()
            for s in bl_in_chapter:
                bl_words.update(s.get("blacklist_hits", []))
            with open(work_dir / FAILED_LIST_FILENAME, "a", encoding="utf-8") as f:
                f.write(f"{chapter_data.get('file', '')} — 黑名单命中: {', '.join(sorted(bl_words))}\n")

        print(f"  [{chapter:04d}] {chapter_data.get('file', '')} "
              f"— {len(proofread_segments)} sent units "
              f"({sum(1 for s in proofread_segments if s.get('blacklisted'))} blacklisted)")

    print(f"  Dump text for LLM: python proofread.py dump-text {work_dir}")
    print(f"\n  Summary: {total_blacklisted} segments flagged, "
          f"{total_sentences} sentences ready for phase C")

    return 0


def _three_way_merge(old_pp_content, corrected_content, new_pp_content, _depth=0):
    """Apply LLM edits (old_pp→corrected) onto new preprocessed text.

    Uses difflib to compute the LLM's changes relative to the OLD
    preprocessed base, then applies those same edits to the NEW
    preprocessed base. This prevents cascading double-replacement when
    a glossary translation contains another glossary term (叠字 bug).

    The merge works by:
      1. Diffing old_pp vs corrected to find LLM's edit operations
      2. Diffing old_pp vs new_pp to map character positions
      3. Applying LLM's edit operations to the corresponding positions
         in new_pp, using the position map from step 2

    Args:
        old_pp_content: Old preprocessed text (base both deltas are from)
        corrected_content: LLM-corrected text (base + LLM edits)
        new_pp_content: New preprocessed text (base + glossary updates)
        _depth: Internal recursion guard (max 3 levels)

    Returns:
        Merged text with both glossary updates and LLM edits.
    """
    if corrected_content == old_pp_content:
        return new_pp_content

    # Build position map: old_pp[i] → new_pp[j]
    align = difflib.SequenceMatcher(None, old_pp_content, new_pp_content)

    def _map_pos(old_pos):
        """Map a character position from old_pp to new_pp."""
        prev_j2 = 0
        for tag, i1, i2, j1, j2 in align.get_opcodes():
            if i1 <= old_pos < i2:
                if tag == 'equal':
                    return j1 + (old_pos - i1)
                elif tag == 'replace':
                    ratio = (old_pos - i1) / max(i2 - i1, 1)
                    return int(j1 + ratio * (j2 - j1))
                elif tag == 'delete':
                    return j1
                elif tag == 'insert':
                    return j2
            elif old_pos == i2:
                # Position is at the boundary between blocks.
                # Map to the end of the corresponding block in new_pp.
                if tag == 'equal':
                    prev_j2 = j2
                elif tag == 'replace':
                    prev_j2 = j2
                elif tag == 'delete':
                    prev_j2 = j1  # deleted text maps to insertion point
            elif old_pos == i1 and old_pos == i2:
                return prev_j2
            prev_j2 = j2
        return prev_j2

    # Build result by applying LLM edits to new_pp
    edits = difflib.SequenceMatcher(None, old_pp_content, corrected_content)
    result = []
    new_pos = 0

    for tag, i1, i2, j1, j2 in edits.get_opcodes():
        if tag == 'equal':
            # LLM kept this chunk. Map to new_pp.
            mapped_start = _map_pos(i1)
            mapped_end = _map_pos(i2)
            if mapped_start is not None and mapped_end is not None:
                if new_pos < mapped_start:
                    result.append(new_pp_content[new_pos:mapped_start])
                result.append(new_pp_content[mapped_start:mapped_end])
                new_pos = mapped_end
        elif tag == 'replace':
            # LLM replaced old_pp[i1:i2] with corrected[j1:j2].
            # Map the replace range to new_pp and apply the LLM's text.
            mapped_start = _map_pos(i1)
            mapped_end = _map_pos(i2)
            if mapped_start is not None and mapped_end is not None:
                if new_pos < mapped_start:
                    result.append(new_pp_content[new_pos:mapped_start])
                new_pp_chunk = new_pp_content[mapped_start:mapped_end]
                old_chunk = old_pp_content[i1:i2]
                llm_chunk = corrected_content[j1:j2]
                if new_pp_chunk != old_chunk:
                    # Glossary already changed this region.
                    # If LLM's change is unrelated to the glossary change,
                    # apply LLM's edit to new_pp_chunk using a sub-merge.
                    if _depth >= 3:
                        result.append(llm_chunk)  # max recursion, prefer LLM
                    else:
                        sub = _three_way_merge(old_chunk, llm_chunk, new_pp_chunk, _depth + 1)
                        result.append(sub)
                else:
                    result.append(llm_chunk)
                new_pos = mapped_end
        elif tag == 'delete':
            # LLM deleted this chunk. Skip it in result.
            mapped_start = _map_pos(i1)
            mapped_end = _map_pos(i2)
            if mapped_start is not None and mapped_end is not None:
                if new_pos < mapped_start:
                    result.append(new_pp_content[new_pos:mapped_start])
                new_pos = mapped_end
        elif tag == 'insert':
            # LLM inserted text. Place it at the current position.
            result.append(corrected_content[j1:j2])

    if new_pos < len(new_pp_content):
        result.append(new_pp_content[new_pos:])

    return ''.join(result)


def cmd_prepare_round3(args):
    """Prepare batches for Round 3 literary polishing.

    Regenerates batch text files (clean) and writes Round 3 mechanical
    markers to _checklist.json files. This prevents the LLM from facing
    an empty checklist in Round 3, which would invite tunnel-vision —
    the LLM would see "clean" text and find nothing to do.

    Round 3 mechanical markers include:
      - 翻译腔 patterns: 被……所……, 开始……起来, ……着……着
      - 数字: Arabic numerals to convert to Chinese
      - 长句: sentences >100 chars (euro-chinese splitting candidates)
      - 粘滞: consecutive identical CJK chars (comma insertion candidates)

    These are regex-level HINTS, not auto-corrections. The LLM must
    still exercise judgment — markers may be false positives.
    """
    work_dir = Path(args.work_dir)

    if not (work_dir / "extracted" / "index.json").exists():
        print("  No index.json found. Run pipeline first.")
        return 1

    # Persist round3 in config so subsequent _redump_batches calls
    # (e.g. from reprocess) also include Round 3 markers.
    config = load_config(work_dir)
    config.setdefault("proofreading", {})["round3"] = True
    save_config(config, work_dir)

    _redump_batches(work_dir, round3=True)

    # Count markers for user feedback
    batch_dir = work_dir / "proofread_batches"
    total_markers = 0
    for cf in sorted(batch_dir.glob("batch_*_checklist.json")):
        with open(cf, "r", encoding="utf-8") as f:
            checklist = json.load(f)
        total_markers += len(checklist)

    print(f"  Round 3 checklist: {total_markers} annotated segments across all batches")
    print(f"  Ready for literary polishing.")
    return 0


def cmd_reprocess(args, new_terms=None):
    """Re-run preprocess with updated glossary (second pass).

    Use case: Claude identifies new term inconsistencies during phase C
    and adds them to glossary via add-term/add-terms. Then reprocess
    applies the enriched glossary mechanically to ALL chapters.

    Reads original extracted text (chapter_NNNN.json), re-applies
    proofread_text with current glossary, and overwrites _preprocessed.json.

    When new_terms is provided (set of term strings), only chapters whose
    original text contains at least one new term are reprocessed. Chapters
    without any new term are skipped, avoiding wasted I/O for large books.
    """
    work_dir = Path(args.work_dir)
    extracted_dir = work_dir / "extracted"

    index_path = extracted_dir / "index.json"
    if not index_path.exists():
        print("  No index.json found.")
        return 1

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    glossary = load_glossary(work_dir)
    _rebuild_glossary_regex(glossary)
    config = load_config(work_dir)
    blacklist = config.get("blacklist", [])
    advisory_blacklist = config.get("blacklist_advisory", config.get("advisory", []))
    threshold = config.get("proofreading", {}).get("long_text_threshold", 300)
    split_punc = config.get("proofreading", {}).get("split_punctuation", ["。", "？", "！"])

    total_blacklisted = 0
    total_sentences = 0
    merged_segments = 0

    for item in index:
        chapter = item["chapter"]
        orig_path = extracted_dir / f"chapter_{chapter:04d}.json"
        if not orig_path.exists():
            continue

        preproc_path = extracted_dir / f"chapter_{chapter:04d}_preprocessed.json"
        corr_path = extracted_dir / f"chapter_{chapter:04d}_corrected.json"
        corr_sentinel = extracted_dir / f"chapter_{chapter:04d}.corrected"

        # Read OLD _preprocessed.json BEFORE overwriting — needed as the
        # base for 3-way merge of _corrected.json content.
        old_preproc_data = None
        if preproc_path.exists() and corr_path.exists() and corr_sentinel.exists():
            try:
                with open(preproc_path, "r", encoding="utf-8") as f:
                    old_preproc_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        with open(orig_path, "r", encoding="utf-8") as f:
            chapter_data = json.load(f)

        # Selective reprocess: skip chapters without new terms
        if new_terms:
            ch_text = "".join(s.get("content", "") for s in chapter_data.get("segments", []))
            if not any(t in ch_text for t in new_terms):
                continue

        proofread_segments = []
        quote_state = {"left": True}  # persist quote parity across segments
        for seg in chapter_data.get("segments", []):
            original = seg.get("content", "")
            processed, blacklisted, bl_hits, advisory_hits = proofread_text(
                original, glossary, blacklist, quote_state, advisory_blacklist
            )
            if blacklisted:
                total_blacklisted += 1
            # Flag English-heavy segments (same logic as cmd_preprocess)
            is_english = _is_english_heavy(processed)
            sentences = split_long_text(processed, threshold, split_punc)
            total_sentences += len(sentences)

            for j, sentence in enumerate(sentences):
                proofread_segments.append({
                    "id": seg["id"],
                    "sub_id": j,
                    "type": seg.get("type", "text"),
                    "original": original if j == 0 else "",
                    "content": sentence,
                    "blacklisted": blacklisted,
                    "blacklist_hits": bl_hits,
                    "advisory_hits": advisory_hits,
                    "is_english": is_english,
                })

        out = {
            "chapter": chapter,
            "file": chapter_data.get("file", ""),
            "segments": proofread_segments,
            "_note": "Reprocessed with updated glossary. Claude: apply phase C proofreading."
        }
        with open(preproc_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

        print(f"  [{chapter:04d}] {chapter_data.get('file', '')} — reprocessed "
              f"({sum(1 for s in proofread_segments if s.get('blacklisted'))} blacklisted)")

        # Update _corrected.json via 3-way merge, not by re-applying glossary.
        # Re-applying proofread_text to already-processed text causes cascading
        # double-replacement (the 叠字 bug) when a translation contains another
        # glossary term. The merge computes LLM edits relative to old preprocessed
        # and applies them to new preprocessed, avoiding the cascade.
        if old_preproc_data is not None and corr_path.exists() and corr_sentinel.exists():
            with open(corr_path, "r", encoding="utf-8") as f:
                corr_data = json.load(f)

            # Build lookup maps keyed by segment + sub_id
            old_pp_map = {}
            for s in old_preproc_data.get("segments", []):
                key = f"{s['id']}.{s.get('sub_id', 0)}"
                old_pp_map[key] = s.get("content", "")

            new_pp_map = {}
            for s in proofread_segments:
                key = f"{s['id']}.{s.get('sub_id', 0)}"
                new_pp_map[key] = s.get("content", "")

            for seg in corr_data.get("segments", []):
                key = f"{seg['id']}.{seg.get('sub_id', 0)}"
                old_pp = old_pp_map.get(key, "")
                new_pp = new_pp_map.get(key, "")
                corrected = seg.get("content", "")

                if not old_pp or not new_pp:
                    continue
                if old_pp == new_pp:
                    continue  # No glossary change for this segment
                if corrected == old_pp:
                    seg["content"] = new_pp  # No LLM edits, use new pp
                    merged_segments += 1
                else:
                    # Safety checks before 3-way merge:
                    # 1. Very short segments (< 15 chars) risk character-level
                    #    merge artifacts (single-char diffs → garbled output
                    #    in 0.4-0.6 LCS range). Keep LLM version as-is.
                    # 2. Heavy rewrites (LCS < 0.45): skip merge, keep LLM
                    #    version. Character-level merge on restructured
                    #    Chinese sentences produces truncated/incorrect output.
                    if len(corrected) < 15:
                        # Too short for safe 3-way merge, but still apply
                        # glossary regex so short segments get term updates.
                        # (Without this, old terms in short dialogue like
                        # "好"/"走吧" survive reprocess indefinitely.)
                        if glossary and _GLOSSARY_REGEX.pattern:
                            updated, cnt = _GLOSSARY_REGEX.subn(
                                lambda m: glossary.get(m.group(0), m.group(0)),
                                corrected)
                            if cnt > 0:
                                seg["content"] = updated
                                merged_segments += 1
                    else:
                        lcs = difflib.SequenceMatcher(None, old_pp, corrected).ratio()
                        if lcs < 0.45:
                            pass  # keep LLM version, heavy rewrite
                        else:
                            merged = _three_way_merge(old_pp, corrected, new_pp)
                            if merged != corrected:
                                seg["content"] = merged
                                merged_segments += 1

            tmp_path = extracted_dir / f"chapter_{chapter:04d}_corrected.json.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(corr_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, corr_path)

    if merged_segments:
        print(f"  _corrected.json: {merged_segments} segments 3-way merged with updated glossary")

    print(f"\n  Reprocess complete: {total_blacklisted} flagged, {total_sentences} sentences")

    # Regenerate batch files so LLM sees fresh text with latest glossary
    _redump_batches(args.work_dir)

    return 0


def cmd_check(args):
    """Run mechanical checks on proofread output. With --fix, auto-revert safe violations.

    Validates:
      1. Change ratio per segment (must be ≤ max_change_ratio)
      2. Hard blacklist words have been replaced
      3. Advisory blacklist words are informational only

    With --fix: auto-reverts over-changed mechanical/preprocessed segments
    to a conservative version. LLM-corrected segments are never overwritten.
    """
    work_dir = Path(args.work_dir)
    extracted_dir = work_dir / "extracted"
    config = load_config(work_dir)
    do_fix = getattr(args, 'fix', False)

    index_path = extracted_dir / "index.json"
    if not index_path.exists():
        print("  No index.json found.")
        return 1

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    total_violations = 0
    total_reverted = 0
    chapters_to_check = index
    if hasattr(args, 'chapter') and args.chapter is not None:
        try:
            c = int(args.chapter)
            chapters_to_check = [item for item in index if item["chapter"] == c]
        except ValueError:
            chapters_to_check = index

    for item in chapters_to_check:
        chapter = item["chapter"]

        # Prefer corrected (LLM) > preprocessed (glossary+style) > raw extract.
        # Must also check .corrected sentinel: _corrected.json can be stale
        # (left over from a previous apply-corrections whose sentinel was
        # reset by a subsequent reprocess with new glossary terms).
        corr_path = extracted_dir / f"chapter_{chapter:04d}_corrected.json"
        corr_sentinel = extracted_dir / f"chapter_{chapter:04d}.corrected"
        prep_path = extracted_dir / f"chapter_{chapter:04d}_preprocessed.json"
        raw_path = extracted_dir / f"chapter_{chapter:04d}.json"

        if corr_path.exists() and corr_sentinel.exists():
            proofread_path = corr_path
        elif prep_path.exists():
            proofread_path = prep_path
        elif raw_path.exists():
            proofread_path = raw_path
        else:
            continue

        with open(proofread_path, "r", encoding="utf-8") as f:
            proofread_data = json.load(f)

        # Group segments by id — sub-segments must be compared as whole
        # paragraphs. Comparing sub_id=0's 50-char sentence against the
        # 300-char original produces false 80%+ change ratios, and --fix
        # would replace the sentence with the full paragraph (data duplication).
        segs = proofread_data.get("segments", [])
        has_subs = any("sub_id" in s for s in segs)
        if has_subs:
            grouped = {}
            for idx, s in enumerate(segs):
                grouped.setdefault(s["id"], []).append((idx, s))
            seg_groups = [(sid, [(i, d) for i, d in parts]) for sid, parts in grouped.items()]
        else:
            seg_groups = [(s["id"], [(idx, s)]) for idx, s in enumerate(segs)]

        threshold = config.get("proofreading", {}).get("long_text_threshold", 300)
        split_punc = config.get("proofreading", {}).get("split_punctuation", ["。", "？", "！"])

        # Auto-fix if requested (operates on grouped data).
        # Use per-chapter counter — total_reverted spans all chapters
        # and would cause false file rewrites for subsequent chapters.
        violations = []
        chapter_reverted = 0
        if do_fix:
            for sid, parts in seg_groups:
                # parts = [(idx_in_segs, dict), ...]
                first_seg = parts[0][1]
                if first_seg.get("blacklisted"):
                    continue
                full_original = first_seg.get("original", "")
                if not full_original:
                    continue
                full_proofread = "".join(p[1].get("content", "") for p in parts)
                if full_original == full_proofread:
                    continue
                change_ratio = compute_change_ratio(full_original, full_proofread)
                max_change = config.get("proofreading", {}).get("max_change_ratio", 0.4)
                if change_ratio > max_change:
                    # If this is _corrected.json (LLM-proofread), NEVER
                    # overwrite LLM corrections with mechanical-only text.
                    # The high change ratio is often intentional (e.g.,
                    # translation-ese rewriting). Only auto-fix preprocessed
                    # (mechanical-only) data.
                    if proofread_path == corr_path:
                        violations.append(
                            f"  seg [{sid}] "
                            f"change ratio {change_ratio:.2f} exceeds max {max_change} "
                            f"(LLM-corrected segment; --fix skipped to preserve corrections)"
                        )
                    else:
                        conservative = apply_mechanical_style_fixes(full_original)
                        sentences = split_long_text(conservative, threshold, split_punc)
                        # Do NOT insert in-place — that would shift indices for
                        # other seg_groups whose cached indices become stale.
                        # Instead, append to end and sort (id, sub_id) afterward.
                        for j, sent in enumerate(sentences):
                            if j < len(parts):
                                parts[j][1]["content"] = sent
                            else:
                                new_part = dict(parts[0][1])
                                new_part["sub_id"] = j
                                new_part["content"] = sent
                                new_part["original"] = ""
                                segs.append(new_part)
                        for j in range(len(sentences), len(parts)):
                            parts[j][1]["content"] = ""
                        total_reverted += 1
                        chapter_reverted += 1
            if chapter_reverted:
                segs.sort(key=lambda x: (x["id"], x.get("sub_id", 0)))
                with open(proofread_path, "w", encoding="utf-8") as f:
                    json.dump(proofread_data, f, ensure_ascii=False, indent=2)

        # Check violations on grouped data
        for sid, parts in seg_groups:
            # parts = [(idx_in_segs, dict), ...]
            first_seg = parts[0][1]
            if first_seg.get("blacklisted"):
                bl_hits = first_seg.get("blacklist_hits", [])
                full_content = "".join(p[1].get("content", "") for p in parts)
                still_present = [w for w in bl_hits if w in full_content]
                if still_present:
                    violations.append(
                        f"  seg [{sid}] "
                        f"BLACKLIST words not yet replaced: {still_present}"
                    )
                continue

            full_original = first_seg.get("original", "")
            if not full_original:
                full_original = "".join(p[1].get("content", "") for p in parts)
            full_proofread = "".join(p[1].get("content", "") for p in parts)
            if full_original and full_proofread:
                passed, issues = mechanical_proofreading_checks(full_original, full_proofread, config)
                if not passed:
                    violations.append(
                        f"  seg [{sid}] "
                        f"{' | '.join(issues)}"
                    )

        if violations:
            total_violations += len(violations)
            print(f"  [{chapter:04d}] {item.get('file', '')} — {len(violations)} violation(s):")
            for v in violations:
                print(v)

    # Diff report (--diff / --diff-log flags).
    # Terminal output is spoiler-safe (counts only). Use --diff-log FILE
    # for detailed before/after text suitable for human or LLM review.
    do_diff = getattr(args, 'diff', False)
    diff_log = getattr(args, 'diff_log', None)
    if do_diff or diff_log:
        total_changed = 0
        total_chars_changed = 0
        log_lines = []  # detailed diff for file output
        if do_diff:
            print(f"\n  === Diff report ===")
        for item in chapters_to_check:
            chapter = item["chapter"]
            orig_path = extracted_dir / f"chapter_{chapter:04d}_preprocessed.json"
            corr_path = extracted_dir / f"chapter_{chapter:04d}_corrected.json"
            corr_sentinel = extracted_dir / f"chapter_{chapter:04d}.corrected"
            if corr_path.exists() and corr_sentinel.exists():
                proof_path = corr_path
            else:
                proof_path = extracted_dir / f"chapter_{chapter:04d}.json"
                if not proof_path.exists():
                    proof_path = orig_path
            if not orig_path.exists() and proof_path.exists():
                continue
            if orig_path == proof_path:
                continue
            with open(orig_path, encoding='utf-8') as f:
                orig_data = json.load(f)
            with open(proof_path, encoding='utf-8') as f:
                proof_data = json.load(f)
            ch_changes = 0
            orig_map = {}
            for s in orig_data.get("segments", []):
                sid = s.get("id")
                if "sub_id" in s:
                    orig_map.setdefault(sid, []).append(s.get("content", ""))
                else:
                    orig_map[sid] = s.get("content", "")
            for sid in orig_map:
                if isinstance(orig_map[sid], list):
                    orig_map[sid] = "".join(orig_map[sid])

            proof_map = {}
            for s in proof_data.get("segments", []):
                sid = s.get("id")
                if "sub_id" in s:
                    proof_map.setdefault(sid, []).append(s.get("content", ""))
                else:
                    proof_map[sid] = s.get("content", "")
            for sid in proof_map:
                if isinstance(proof_map[sid], list):
                    proof_map[sid] = "".join(proof_map[sid])

            ch_details = []
            for sid in orig_map:
                if sid in proof_map and orig_map[sid] != proof_map[sid]:
                    ch_changes += 1
                    total_chars_changed += abs(len(proof_map[sid]) - len(orig_map[sid]))
                    if diff_log:
                        ch_details.append((sid, orig_map[sid], proof_map[sid]))
            if ch_changes > 0:
                if do_diff:
                    print(f"  [{chapter:04d}] {item.get('file', '')}: {ch_changes} segments modified")
                if ch_details:
                    log_lines.append(f"\n{'='*60}")
                    log_lines.append(f"CHAPTER {chapter}: {item.get('file', '')} ({ch_changes} modified)")
                    log_lines.append(f"{'='*60}")
                    for sid, orig_txt, proof_txt in ch_details:
                        log_lines.append(f"\n--- seg {sid} ---")
                        log_lines.append(f"- {orig_txt}")
                        log_lines.append(f"+ {proof_txt}")
            total_changed += ch_changes
        if do_diff:
            print(f"  Total: {total_changed} segments modified (~{total_chars_changed} char delta)")

        if diff_log:
            log_path = Path(diff_log)
            spoiler_header = [
                "=" * 60,
                "  SPOILER WARNING — 剧情剧透警告",
                "  本文件包含小说原文与校对后的逐句对比。",
                "  如果你还没有读完这本书，请立即关闭此文件。",
                "=" * 60,
                "",
            ]
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(spoiler_header + log_lines))
            print(f"  Diff log: {log_path} ({total_changed} segments, spoiler warning included)")

    if total_reverted:
        print(f"\n  Auto-fixed: {total_reverted} segments reverted to conservative version.")
    if total_violations == 0:
        print("  All checks passed.")
    else:
        print(f"\n  Total violations: {total_violations}")

    # --glossary: verify glossary coverage in HTML/XHTML files.
    # Checks whether any glossary source keys still appear as raw text in the
    # final output — indicating terms that the LLM or mechanical passes missed.
    # Also validates the glossary itself for doubled-CJK targets (corrupted
    # canonical forms like 野野蕾薇院).
    do_glossary = getattr(args, 'glossary', False)
    if do_glossary:
        glossary = load_glossary(work_dir)
        if not glossary:
            print("\n  [glossary check] No glossary found.")
        else:
            # --- Self-validation: scan glossary for doubled-CJK targets ---
            bad_targets = []
            for term, target in glossary.items():
                if term == target:
                    continue
                doubled = re.findall(r'([\u4e00-\u9fff])\1', target)
                if doubled:
                    bad_targets.append((term, target, set(doubled)))
            if bad_targets:
                print(f"\n  === Glossary self-check: {len(bad_targets)} entries with doubled CJK in target ===")
                for term, target, chars in bad_targets:
                    print(f"  {term} → {target}  (doubled: {', '.join(c*2 for c in chars)})")
                print("  These targets are likely LLM output errors. Fix them with add-term or add-terms.")

            # Collect all text content from HTML/XHTML files
            html_text = ""
            html_files = sorted(
                list(Path(work_dir).rglob("*.xhtml")) +
                list(Path(work_dir).rglob("*.html")) +
                list(Path(work_dir).rglob("*.htm"))
            )
            for fp in html_files:
                if any(p in ("extracted", "proofread_batches") for p in fp.parts):
                    continue
                content, _ = _read_xhtml_text(fp)
                html_text += content

            # Check each CJK glossary key for residual occurrences
            residual = []
            for term, target in glossary.items():
                # Only check CJK→CJK entries (ASCII keys like "Kushiel"→"库希尔"
                # are already handled by English-term deletion in earlier passes)
                if term == target:
                    continue
                if any(c.isascii() and c.isalpha() for c in term):
                    continue
                count = _count_glossary_residual(html_text, term, target)
                if count > 0:
                    residual.append((term, target, count))

            if residual:
                residual.sort(key=lambda x: -x[2])  # most frequent first
                print(f"\n  === Glossary coverage check: {len(residual)} un-replaced terms ===")
                for term, target, count in residual[:30]:
                    print(f"  [{count:4d}] {term} → {target}")
                if len(residual) > 30:
                    print(f"  ... and {len(residual) - 30} more")
            else:
                print("\n  [glossary check] All CJK glossary terms applied — no residuals.")

    return 0 if total_violations == 0 else 1


def cmd_extract_terms(args):
    """Auto-extract newly unified terms by comparing original vs proofread text.

    After Claude does phase C proofreading, this command scans for consistent
    replacements across multiple segments and suggests glossary additions.

    How it works: if "亚拉冈" appears in original text but is consistently
    replaced with "阿拉贡" across ≥3 segments, extract the mapping.
    """
    work_dir = Path(args.work_dir)
    extracted_dir = work_dir / "extracted"
    glossary = load_glossary(work_dir)

    index_path = extracted_dir / "index.json"
    if not index_path.exists():
        print("  No index.json found.")
        return 1

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    # Collect original→proofread pairs from all chapters
    import collections
    replacements = collections.Counter()  # (original_word, proofread_word) → count

    for item in index:
        chapter = item["chapter"]
        # Read original extracted text
        orig_path = extracted_dir / f"chapter_{chapter:04d}.json"
        # Read proofread version (prefer LLM-corrected > raw > preprocessed)
        corr_path = extracted_dir / f"chapter_{chapter:04d}_corrected.json"
        proof_path = extracted_dir / f"chapter_{chapter:04d}.json"
        pp_path = extracted_dir / f"chapter_{chapter:04d}_preprocessed.json"

        if not orig_path.exists():
            continue

        with open(orig_path, "r", encoding="utf-8") as f:
            orig_data = json.load(f)

        # Check sentinel — _corrected.json can be stale after reprocess
        corr_sentinel = extracted_dir / f"chapter_{chapter:04d}.corrected"
        valid_corr = corr_path.exists() and corr_sentinel.exists()

        proof_data = None
        candidates = [corr_path] if valid_corr else []
        candidates.extend([proof_path, pp_path])
        for p in candidates:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    proof_data = json.load(f)
                break

        if not proof_data:
            continue

        # Group proofread segments by id (handles sub-segmented data).
        # orig_data has full paragraphs; proof_data may have sub-segments.
        # Must join sub-segments before comparing against full original.
        proof_by_id = {}
        for seg in proof_data.get("segments", []):
            proof_by_id.setdefault(seg["id"], []).append(seg.get("content", ""))

        orig_segs = {s["id"]: s["content"] for s in orig_data.get("segments", [])}
        for sid, parts in proof_by_id.items():
            if sid not in orig_segs:
                continue
            orig_text = orig_segs[sid]
            proof_text = "".join(parts)

            if orig_text == proof_text:
                continue

            # Use difflib to find actual replacements (robust against offset drift)
            from difflib import SequenceMatcher
            sm = SequenceMatcher(None, orig_text, proof_text)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == 'replace':
                    sub_orig = orig_text[i1:i2]
                    sub_proof = proof_text[j1:j2]
                    # Only capture Chinese-only replacements of 2-5 chars
                    if (2 <= len(sub_orig) <= 10 and 2 <= len(sub_proof) <= 10 and
                        abs(len(sub_orig) - len(sub_proof)) <= 3 and
                        all(_is_valid_term_char(c) for c in sub_orig + sub_proof) and
                        sub_orig not in glossary and sub_proof not in glossary):
                        replacements[(sub_orig, sub_proof)] += 1

    # Filter: keep pairs that appear ≥3 times (consistent replacement)
    candidates = [(pair, count) for pair, count in replacements.items() if count >= 3]
    candidates.sort(key=lambda x: x[1], reverse=True)

    if candidates:
        print(f"  Auto-detected {len(candidates)} potential glossary additions:")
        terms_list = []
        for (orig, proof), count in candidates[:20]:
            print(f"    {orig} → {proof}  ({count}x)")
            terms_list.append({"term": orig, "translation": proof})

        print(f"\n  To add these, run:")
        print(f"    python proofread.py add-terms {work_dir} '{json.dumps(terms_list, ensure_ascii=False)}'")
    else:
        print("  No consistent replacements detected (need ≥3 occurrences).")

    return 0


def cmd_inject(args):
    """Apply proofread segments back to XHTML files using binary replacement.

    Does NOT use lxml tree.write() — instead:
      1. lxml parses to find .text/.tail positions
      2. Raw file bytes are modified in-place (binary string replace)
      3. This preserves original serialization perfectly (no namespace reordering,
         no self-closing tag flattening, no entity changes).

    Before injecting, restores XHTML files from the source EPUB (if available
    via context.json) to guarantee clean original text for binary matching.
    Previous pack runs may have modified these files via glossary application,
    making segment original text un-findable on subsequent inject passes.
    """
    work_dir = Path(args.work_dir)
    extracted_dir = work_dir / "extracted"

    # --- Restore XHTML from source EPUB before injecting ---
    context_path = work_dir / "context.json"
    if context_path.exists():
        with open(context_path, "r", encoding="utf-8") as f:
            ctx = json.load(f)
        input_epub = ctx.get("input_epub")
        if input_epub and os.path.exists(input_epub):
            import zipfile as _zipfile
            with _zipfile.ZipFile(input_epub) as _zf:
                restored = 0
                for _n in _zf.namelist():
                    if _n.endswith('.html') or _n.endswith('.xhtml') or _n.endswith('.htm'):
                        if not _is_safe_epub_member(_n):
                            raise ValueError(f"Unsafe EPUB entry path: {_n}")
                        # Use full ZIP internal path, not basename.
                        # EPUBs commonly store XHTML in OEBPS/Text/ etc.
                        _dest = work_dir / _n
                        if _dest.exists():
                            _zf.extract(_n, str(work_dir))
                            restored += 1
                if restored:
                    print(f"  Restored {restored} XHTML files from source EPUB")

    # --- Pre-inject safety net: catch English segments the LLM missed ---
    _cleanup_residual_english(work_dir)

    index_path = extracted_dir / "index.json"
    if not index_path.exists():
        print("  No index.json found. Run extract first.")
        return 1

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    total_skipped = 0
    for item in index:
        chapter = item["chapter"]
        rel_path = item["file"]
        fp = work_dir / rel_path

        # Prefer corrected (LLM phase C) > preprocessed (glossary+style) > raw extract.
        # Must also check .corrected sentinel: _corrected.json can be stale.
        corrected_path = extracted_dir / f"chapter_{chapter:04d}_corrected.json"
        corr_sentinel = extracted_dir / f"chapter_{chapter:04d}.corrected"
        preproc_path = extracted_dir / f"chapter_{chapter:04d}_preprocessed.json"
        raw_path = extracted_dir / f"chapter_{chapter:04d}.json"
        if corrected_path.exists() and corr_sentinel.exists():
            proofread_path = corrected_path
        elif preproc_path.exists():
            proofread_path = preproc_path
        elif raw_path.exists():
            proofread_path = raw_path
        else:
            print(f"  [{chapter:04d}] {rel_path} — no proofread file, skipped")
            continue

        with open(proofread_path, "r", encoding="utf-8") as f:
            proofread_data = json.load(f)

        # Parse with lxml to find text/tail positions, but DON'T serialize back
        try:
            tree, root, etree_mod = parse_xhtml(fp)
        except Exception as e:
            print(f"  [{chapter:04d}] PARSE ERROR: {rel_path} — {e}")
            continue

        # Build replacement map from proofread segments.
        # Key insight: for glossary-modified segments, use the ORIGINAL
        # text as binary-find key (raw XHTML still has pre-glossary text).
        segs = proofread_data.get("segments", [])
        has_subs = any("sub_id" in s for s in segs)
        if has_subs:
            grouped_orig = {}
            grouped_content = {}
            for s in segs:
                grouped_orig.setdefault(s["id"], []).append(
                    s.get("original", "") if s.get("sub_id", 0) == 0 else ""
                )
                grouped_content.setdefault(s["id"], []).append(s.get("content", ""))
            proofread_map = {
                sid: {
                    "orig": "".join(grouped_orig.get(sid, [])),
                    "repl": "".join(grouped_content.get(sid, []))
                }
                for sid in set(list(grouped_orig.keys()) + list(grouped_content.keys()))
            }
        else:
            proofread_map = {
                s["id"]: {
                    "orig": s.get("original", s.get("content", "")),
                    "repl": s.get("content", "")
                }
                for s in segs
            }

        # Walk XHTML tree, collect text/tail nodes in document order.
        # Must apply the SAME filtering as extract_text_segments to keep
        # seg_id counters synchronized. Otherwise proofread_map lookups
        # return wrong values and CSS code gets replaced with novel text.
        # Use (orig, repl) tuples with position-aware find() to handle
        # duplicate text correctly — identical orig text can appear at
        # multiple positions with different replacements (or none).
        all_segments = []  # [(original_str, replacement_str), ...]
        seg_id = 0
        for element in root.iter():
            # Skip Comment/PI .text (same as extract_text_segments)
            if not isinstance(element.tag, str):
                if element.tail and element.tail.strip():
                    orig = element.tail
                    entry = proofread_map.get(seg_id, {})
                    repl = entry.get("repl", orig) if isinstance(entry, dict) else entry
                    all_segments.append((orig, repl))
                    seg_id += 1
                continue

            # Match extract_text_segments: skip .text for non-content elements
            tag_name = element.tag.split("}")[-1] if "}" in element.tag else element.tag
            skip_text = tag_name in ('style', 'script', 'title', 'meta')

            if not skip_text and element.text and element.text.strip():
                orig = element.text
                entry = proofread_map.get(seg_id, {})
                repl = entry.get("repl", orig) if isinstance(entry, dict) else entry
                all_segments.append((orig, repl))
                seg_id += 1

            # .tail always collected (same as extract_text_segments)
            if element.tail and element.tail.strip():
                orig = element.tail
                entry = proofread_map.get(seg_id, {})
                repl = entry.get("repl", orig) if isinstance(entry, dict) else entry
                all_segments.append((orig, repl))
                seg_id += 1

        # Binary replacement with position-aware cursor.
        # find(orig, last_idx) ensures each replacement targets the
        # correct occurrence in document order, even when identical
        # text appears at multiple positions and only some changed.
        skipped_local = 0
        if all_segments:
            content, file_encoding = _read_xhtml_text(fp)

            last_idx = 0
            for orig, repl in all_segments:
                orig_len = len(orig)  # capture before mutation for cursor
                idx = content.find(orig, last_idx)
                escaped = False
                entity_form = None  # 'named', 'decimal', or 'hex'
                if idx == -1:
                    # lxml unescapes XML/HTML entities (&amp; → &,
                    # &nbsp; → \xa0, &ldquo; → \u201c, etc.) in element.text,
                    # but raw file bytes retain the original escaped form.
                    orig_escaped = html.escape(orig)
                    if _ENTITY_REVERSE_MAP:
                        for char, entity in _ENTITY_REVERSE_MAP:
                            orig_escaped = orig_escaped.replace(char, entity)
                    idx = content.find(orig_escaped, last_idx)
                    if idx != -1:
                        orig = orig_escaped
                        orig_len = len(orig)
                        escaped = True
                        entity_form = 'named'
                    # Named entities not found — try numeric entities
                    # (&#160; for &nbsp;, &#x2014; for &mdash;). Calibre
                    # and Sigil often emit numeric instead of named entities.
                    if idx == -1 and _ENTITY_REVERSE_MAP:
                        orig_decimal = html.escape(orig)
                        for char, _entity in _ENTITY_REVERSE_MAP:
                            if len(char) != 1:
                                # Parse hex code from &#xNN; to produce &#DEC;
                                m = re.match(r'&#x([0-9a-fA-F]+);', char)
                                if m:
                                    dec_val = int(m.group(1), 16)
                                    orig_decimal = orig_decimal.replace(
                                        char, f'&#{dec_val};'
                                    )
                                continue
                            cp = ord(char)
                            orig_decimal = orig_decimal.replace(
                                char, f'&#{cp};'
                            )
                        idx = content.find(orig_decimal, last_idx)
                        if idx == -1:
                            # Also try hex form: &#xNNNN;
                            orig_hex = html.escape(orig)
                            for char, _entity in _ENTITY_REVERSE_MAP:
                                if len(char) != 1:
                                    continue  # already in hex form
                                cp = ord(char)
                                orig_hex = orig_hex.replace(
                                    char, f'&#x{cp:x};'
                                )
                            idx = content.find(orig_hex, last_idx)
                            if idx != -1:
                                orig = orig_hex
                                orig_len = len(orig)
                                escaped = True
                                entity_form = 'hex'
                        else:
                            orig = orig_decimal
                            orig_len = len(orig)
                            escaped = True
                            entity_form = 'decimal'
                    # CJK numeric entities: some EPUB tools (Sigil, early
                    # Calibre) encode CJK characters as &#xNNNN;. lxml
                    # decodes these to characters, but raw file bytes retain
                    # the entity form. Try hex-encoding all CJK chars.
                    if idx == -1:
                        has_cjk = any(_is_cjk(c) for c in orig)
                        if has_cjk:
                            orig_cjk_hex = html.escape(orig)
                            cjk_buf = list(orig_cjk_hex)
                            i = 0
                            while i < len(cjk_buf):
                                cp = ord(cjk_buf[i])
                                if _is_cjk(cjk_buf[i]):
                                    cjk_buf[i] = f'&#x{cp:x};'
                                i += 1
                            orig_cjk_hex = ''.join(cjk_buf)
                            idx = content.find(orig_cjk_hex, last_idx)
                            if idx != -1:
                                orig = orig_cjk_hex
                                orig_len = len(orig)
                                escaped = True
                                entity_form = 'cjk_hex'
                if idx != -1:
                    if orig != repl:
                        # Always escape for XML safety — LLM may have
                        # introduced &, <, > even if originals were safe.
                        repl_final = html.escape(repl)
                        if escaped and entity_form:
                            for char, _entity in _ENTITY_REVERSE_MAP:
                                if len(char) == 1:
                                    cp = ord(char)
                                    if entity_form == 'decimal':
                                        repl_final = repl_final.replace(char, f'&#{cp};')
                                    elif entity_form in ('hex', 'cjk_hex'):
                                        repl_final = repl_final.replace(char, f'&#x{cp:x};')
                                    else:
                                        repl_final = repl_final.replace(char, _entity)
                                elif entity_form == 'named':
                                    # Multi-char entries: convert Python's &#x27;
                                    # back to EPUB-native &apos; only when the
                                    # source file actually uses named entities.
                                    repl_final = repl_final.replace(char, _entity)
                                # hex/decimal: leave the multi-char entity as-is
                                # (it was already generated in correct form by html.escape)
                        # For CJK entity files, encode ALL CJK in replacement
                        if entity_form == 'cjk_hex':
                            buf = list(repl_final)
                            for i in range(len(buf)):
                                if _is_cjk(buf[i]):
                                    buf[i] = f'&#x{ord(buf[i]):x};'
                            repl_final = ''.join(buf)

                        content = content[:idx] + repl_final + content[idx + len(orig):]
                        last_idx = idx + len(repl_final)
                    else:
                        last_idx = idx + len(repl)
                else:
                    # Last resort: whitespace-normalized regex search.
                    # Preprocessing may normalize \r\n→\n, double spaces→single
                    # between extract and inject. Build a whitespace-flexible
                    # regex from normalized orig to catch these cases.
                    if orig != repl:
                        orig_norm = re.sub(r'\s+', ' ', orig).strip()
                        if orig_norm:
                            pattern = re.escape(orig_norm)
                            pattern = re.sub(r'\\ ', r'\\s+', pattern)
                            m = re.search(pattern, content[last_idx:])
                            if m:
                                idx = last_idx + m.start()
                                orig = m.group()
                                orig_len = len(orig)
                                escaped = False
                                entity_form = None
                            else:
                                skipped_local += 1
                        else:
                            skipped_local += 1
                    else:
                        skipped_local += 1
                    # If still not found after normalization, skip this
                    # segment rather than guessing cursor position — heuristic
                    # advance could cause wrong-text injection on next segment.
            # Write XHTML via temp file to avoid corruption on crash/disk-full.
            # Non-atomic truncation would leave a zero-byte file if interrupted.
            tmp_path = str(fp) + ".tmp"
            with open(tmp_path, "wb") as f:
                try:
                    f.write(content.encode(file_encoding))
                except UnicodeEncodeError:
                    # LLM may have introduced characters outside the original
                    # encoding's repertoire (e.g., CJK Extension chars in GBK).
                    # Fall back to UTF-8 and update ALL encoding declarations.
                    content = re.sub(
                        r'encoding\s*=\s*["\'][^"\']+["\']',
                        'encoding="UTF-8"',
                        content, count=1
                    )
                    content = re.sub(
                        r'(<meta\s+charset\s*=\s*["\'])[^"\']+(["\'])',
                        r'\1UTF-8\2',
                        content, flags=re.IGNORECASE
                    )
                    content = re.sub(
                        r'(<meta\s+[^>]*charset\s*=\s*)[^"\'\s;]+',
                        r'\1UTF-8',
                        content, flags=re.IGNORECASE
                    )
                    f.write(content.encode("utf-8"))
            os.replace(tmp_path, fp)
            total_skipped += skipped_local

        print(f"  [{chapter:04d}] {rel_path} — applied ({skipped_local} skipped)")
    if total_skipped:
        print(f"\n  Warning: {total_skipped} text nodes not found in raw file (replaced via fallback)")
    return 0




def _apply_glossary_to_xhtml(work_dir):
    """Apply ALL glossary translations directly to XHTML text content.

    Handles both ASCII→Chinese (e.g. "Kushiel"→"库希尔") and Chinese→Chinese
    (e.g. "菲德蕾蕾"→"菲德蕾") mappings. This catches terms that were missed
    by inject due to text nodes that couldn't be located in the raw XHTML.

    Uses rglob to find .html/.xhtml in subdirectories (EPUBs commonly place
    them under OEBPS/Text/). Extracts <style>/<script> blocks before regex
    replacement to avoid corrupting CSS/JS, then restores them after.
    """
    glossary = load_glossary(work_dir)
    if not glossary:
        return

    # Split into two sets for reporting, but process all in one regex.
    ascii_terms = {k: v for k, v in glossary.items()
                   if any(c.isascii() and c.isalpha() for c in k)}
    cjk_terms = {k: v for k, v in glossary.items()
                 if not any(c.isascii() and c.isalpha() for c in k)}

    # Build single unified regex (longest-first) for ALL terms.
    # This handles both English→Chinese AND Chinese→Chinese in one pass,
    # preventing inject-skipped segments from retaining old variant forms.
    all_regex = _build_glossary_regex(glossary)

    total = 0
    ascii_count = 0
    cjk_count = 0
    for fpath in sorted(list(Path(work_dir).rglob("*.xhtml")) +
                         list(Path(work_dir).rglob("*.html")) +
                         list(Path(work_dir).rglob("*.htm"))):
        if any(p in ("extracted", "proofread_batches") for p in fpath.parts):
            continue
        content, file_encoding = _read_xhtml_text(fpath)
        original = content

        # Protect <style> and <script> blocks from regex replacement
        protected = {}
        for tag in ("style", "script"):
            for m in re.finditer(
                r"<" + tag + r"[^>]*>.*?</" + tag + r">",
                content, re.DOTALL | re.IGNORECASE
            ):
                placeholder = f"__PROTECTED_{tag}_{len(protected)}__"
                protected[placeholder] = m.group()
                content = content.replace(m.group(), placeholder, 1)

        # Replace within text content only (between > and <).
        # Single-pass regex prevents cascade when one term's translation
        # contains another term.
        def _replace_text(m):
            t = m.group(1)
            if all_regex is not None:
                t, _ = all_regex.subn(lambda m2: glossary.get(m2.group(0), m2.group(0)), t)
            # Note: no space normalization here. Glossary replacement is a
            # clean term→term substitution that doesn't introduce extra
            # whitespace. Previous `re.sub(r" +", " ", t).strip()` destroyed
            # deliberate spacing in poetry, lyrics, and inline-element gaps.
            return ">" + t + "<"

        content = re.sub(r">([^<]+)<", _replace_text, content)

        # Restore protected blocks
        for placeholder, block in protected.items():
            content = content.replace(placeholder, block)

        # Clean empty inline tags left by deleted terms
        for tag in ["i", "b", "em", "strong"]:
            content = re.sub(r"<" + tag + r"[^>]*>\s*</" + tag + ">", "", content)

        if content != original:
            enc = file_encoding
            # Atomic write to prevent truncated XHTML on crash
            tmp_path = Path(str(fpath) + ".tmp")
            try:
                with open(tmp_path, "w", encoding=enc) as f:
                    f.write(content)
            except UnicodeEncodeError:
                # Glossary replacement may have introduced characters outside
                # the original encoding's repertoire (e.g. CJK Extension chars
                # in GBK). Fall back to UTF-8 and update encoding declarations.
                content = re.sub(
                    r'encoding\s*=\s*["\'][^"\']+["\']',
                    'encoding="UTF-8"',
                    content, count=1
                )
                content = re.sub(
                    r'(<meta\s+charset\s*=\s*["\'])[^"\']+(["\'])',
                    r'\1UTF-8\2',
                    content, flags=re.IGNORECASE
                )
                content = re.sub(
                    r'(<meta\s+[^>]*charset\s*=\s*)[^"\'\s;]+',
                    r'\1UTF-8',
                    content, flags=re.IGNORECASE
                )
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(content)
            os.replace(tmp_path, fpath)
            total += 1

    if total:
        print(f"  Glossary->XHTML: {total} files updated ({len(glossary)} terms, ASCII+CJK)")


def cmd_pack(args):
    """Repack EPUB from work directory. Output to project directory if available."""
    work_dir = Path(args.work_dir)
    output_path = Path(args.output_epub) if args.output_epub else None

    # Apply glossary translations to XHTML before packing
    _apply_glossary_to_xhtml(work_dir)

    # Determine output path:
    # Priority: 1) --output-epub flag  2) project_dir from context.json  3) work_dir/..
    if output_path is None:
        context_path = work_dir / "context.json"
        if context_path.exists():
            with open(context_path, "r", encoding="utf-8") as f:
                ctx = json.load(f)
            project_dir = Path(ctx["project_dir"])
            output_path = project_dir / "output.epub"
        else:
            output_path = work_dir.parent / "output.epub"
            print(f"  Warning: context.json missing, output falls back to {output_path}")

    # Ensure mimetype exists
    mimetype_path = work_dir / "mimetype"
    if not mimetype_path.exists():
        print("  Error: mimetype not found in work directory!")
        return 1

    with zipfile.ZipFile(str(output_path), "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype must be first, uncompressed
        zf.write(str(mimetype_path), "mimetype", compress_type=zipfile.ZIP_STORED)

        # All other files
        for root_dir, dirs, files in os.walk(str(work_dir)):
            # Skip work-in-progress directories and files (not part of EPUB)
            dirs[:] = [d for d in dirs if d not in ("extracted", "proofread_batches")]

            for filename in files:
                if filename == "mimetype":
                    continue
                if filename in (GLOSSARY_FILENAME, FAILED_LIST_FILENAME,
                                CONFIG_FILENAME, "context.json",
                                "TASK.md", "full_text.txt", "diff.txt",
                                "voice_cards.md"):
                    continue
                # Skip tooling .json and .md files (EPUB spec only uses .xhtml/.html)
                if filename.endswith(".json") or filename.endswith(".md"):
                    continue

                abs_path = os.path.join(root_dir, filename)
                arc_name = os.path.relpath(abs_path, str(work_dir)).replace(os.sep, '/')
                zf.write(abs_path, arc_name, compress_type=zipfile.ZIP_DEFLATED)

    # Copy persistent files back to project directory
    context_path = work_dir / "context.json"
    if context_path.exists():
        with open(context_path, "r", encoding="utf-8") as f:
            ctx = json.load(f)
        project_dir = Path(ctx["project_dir"])

        # Copy glossary back to project dir (persistent)
        glossary_path = work_dir / GLOSSARY_FILENAME
        if glossary_path.exists():
            shutil.copy2(glossary_path, project_dir / GLOSSARY_FILENAME)
            with open(glossary_path, "r", encoding="utf-8") as f:
                g = json.load(f)
            print(f"  Glossary saved to project ({len(g)} terms)")

        # Copy failed_list
        failed_path = work_dir / FAILED_LIST_FILENAME
        if failed_path.exists():
            shutil.copy2(failed_path, project_dir / FAILED_LIST_FILENAME)

    print(f"  Output: {output_path}")
    return 0


def _find_english_terms(extracted_dir, min_freq=3, top_n=30):
    """Find untranslated English terms that may need glossary entries.

    Scans preprocessed text for ASCII words (2+ letters), filters out
    common English stopwords, and returns high-frequency candidates that
    are likely fantasy terms, character names, or untranslated vocabulary.

    Returns list of (word, frequency) sorted by frequency.
    """
    _STOPWORDS = set("""
        the and of to in was his it my had that he with me you for her
        but not at on is she from as be by we all so were they or this
        have been are no could would did said has one out if its who
        an do up their can them more when what about into
        him there will us then your like some eyes too thought than over
        know face before only knew head back made just now still even
        after where how which our these those such each other through
        between against without within during above below down again
        further once here when while although because since until unless
        however therefore thus hence also both either neither nor yet
        very really quite rather much many few little own same different
        new old first last long great right high low next early young
        large small big good bad true false open close hand way day
        man woman child time year people life world come go see look
        take make give think feel want need seem become leave put mean
        keep let begin show hear play run move live believe hold bring
        happen write sit stand lose pay meet include continue set learn
        change lead understand watch follow stop create speak read spend
        grow walk win offer remember love consider appear buy wait serve
        die send expect build stay fall cut reach kill remain suggest
        raise pass sell require report decide pull toward upon around
        never always often sometimes already soon later perhaps maybe
        well away back off ever almost every another any same own
        should must might shall may been being having doing going
        am are were been being have has had do does did will would
        shall should may might must can could ought need dare used
        this that these those my your his her its our their
        who whom whose which what when where why how
        me him her us them myself yourself himself herself itself
        ourselves themselves
        oh ah yes no well now then so
        said asked replied answered called cried shouted whispered
        told looked turned walked came went stood sat
        something nothing everything anything someone anyone everyone
        somewhere nowhere everywhere anywhere somehow
    """.split())
    import collections
    tokens = collections.Counter()
    for fpath in sorted(extracted_dir.glob("chapter_*_preprocessed.json")):
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for seg in data.get("segments", []):
            text = seg.get("content", "")
            for m in re.finditer(r'(?<![A-Za-z])[A-Za-z]{2,}(?![A-Za-z])', text):
                w_lower = m.group().lower()
                if w_lower not in _STOPWORDS:
                    tokens[w_lower] += 1  # case-insensitive counting
    # Keep words appearing >= min_freq times
    result = [(w, c) for w, c in tokens.most_common(top_n) if c >= min_freq]
    return result


def _find_suspected_variants(extracted_dir, top_n=30):
    """Find potential name variants via shared-character heuristic.

    Returns top-N candidate pairs as (token_a, token_b, combined_freq).

    Optimized: scans tokens once, groups by first-char, then compares
    within groups only. Caps group size to prevent O(n²) blowup on
    high-frequency dictionary characters.
    """
    import collections
    _MAX_GROUP_SIZE = 60
    # Characters commonly used in Chinese transliterations of foreign names.
    # If the last char of a token is NOT in this set, it's likely a verb/particle suffix.
    _TRANSLITERATION_CHARS = set(
        "尔斯特克德拉利里格瑟林恩安伊奥亚维瓦塔诺卡莱蒙布罗纳达马尼加萨巴波索雷弗兰贝哈吉库穆菲珀瑞泰沃温扎赫洛莫佩普鲁塞希修雅约朱丹凯迪艾"
        "琳娜娅妮莉丝蕾黛珊桑瑰琪琦瑶翠芙芬芳蒂蓓薇"
        "昆坦顿敦伦伯格曼森登堡茨兹"
        "阿拜彼茨迦科柯勒梅奈涅帕皮齐日舍施韦沙"
        "耶撒门以兰夫吉麦丹耳列威尼黎但士来百内冰"
    )
    # Tokens starting with these chars are common words, not names
    _COMMON_STARTS = set("我你他她它们这那什谁如为但却而所被把让给向从到自凝因尽即便")
    _SEMANTIC_SUFFIXES = set(
        "人部战式武语大营边掠女各众衣骑寒入冰围军领阵族国城王后"
        "者们级型号地里面上下前后左右内外中间"
        "家兄弟姐妹子儿头手身心口眼脸士师公侯伯爵修卫"
        "一二三四五六七八九十百千万"
        "的地得了着过不只也就还却才又已正把被让给向从到"
        "说道对和与教看想问答笑叫走来去出进回做是在有"
        "我你他她它们这那什谁吗呢吧啊哦嗯么"
        "低轻双以用鞠便躬摇发告诉知高长短好坏多少新旧快慢"
        "脚并逃都见请示可罢即乃予收俯潜拥早永开系措欢足遭覆随未没曾旁将之赞"
    )
    _token_re = re.compile(r'[\u4e00-\u9fff]{2,6}')
    tokens = collections.Counter()
    for fpath in sorted(extracted_dir.glob("chapter_*_preprocessed.json")):
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for seg in data.get("segments", []):
            text = seg.get("content", "")
            for m in _token_re.finditer(text):
                raw = m.group()
                # Skip tokens starting with common words (not names)
                if raw[0] in _COMMON_STARTS:
                    continue
                # Strip trailing non-transliteration char (likely verb/particle)
                if len(raw) >= 3 and raw[-1] not in _TRANSLITERATION_CHARS and raw[-1] not in _SEMANTIC_SUFFIXES:
                    raw = raw[:-1]
                if len(raw) >= 2:
                    tokens[raw] += 1

    def _collapse_boundary_noise(token_counts):
        boundary_chars = set(
            "来向给对把让看望问答说去回走将与和及并着了的地得在是有为以用"
            "再微常亲打太颔耸沉闻似挑举站补深预嫣竟凝背扬琳盛年"
        )
        collapsed = collections.Counter(token_counts)
        for tok, count in list(token_counts.items()):
            if len(tok) < 3:
                continue
            base = tok[:-1]
            if base in token_counts and tok[-1] in boundary_chars:
                collapsed[base] += count
                del collapsed[tok]
        return collapsed

    tokens = _collapse_boundary_noise(tokens)

    # Keep tokens appearing ≥2 times, plus singletons sharing 2-char prefix
    freq2 = {t: c for t, c in tokens.items() if c >= 2}
    # Build prefix index for singletons
    prefix_idx = collections.defaultdict(set)
    for t in freq2:
        if len(t) >= 2:
            prefix_idx[t[:2]].add(t)
    for t, c in tokens.items():
        if t in freq2 or len(t) < 2:
            continue
        if t[-1] in _SEMANTIC_SUFFIXES and t[-1] not in _TRANSLITERATION_CHARS:
            continue
        hits = sum(1 for ch in t if ch in _TRANSLITERATION_CHARS)
        if hits < max(2, math.ceil(len(t) * 0.6)):
            continue
        if t[:2] in prefix_idx:
            freq2[t] = 2  # rescued singleton; use 2 to clear the ≥2 threshold

    if len(freq2) < 2:
        return []

    # Group by first char, cap size
    by_first = collections.defaultdict(list)
    for t in sorted(freq2, key=lambda x: -len(x)):
        g = by_first[t[0]]
        if len(g) < _MAX_GROUP_SIZE:
            g.append(t)

    candidates = []
    pinyin_pairs = set()
    pinyin_scores = {}
    for _char, group in by_first.items():
        if len(group) < 2:
            continue
        gsorted = sorted(group, key=len)
        for i in range(len(gsorted)):
            a = gsorted[i]
            for j in range(i + 1, len(gsorted)):
                b = gsorted[j]
                if a == b:
                    continue
                if len(a) == len(b):
                    shared = sum(1 for k in range(len(a)) if a[k] == b[k])
                    if shared >= len(a) - 1:
                        # Filter: semantic suffix in diff position
                        diff_chars = [a[k] for k in range(len(a)) if a[k] != b[k]]
                        diff_chars += [b[k] for k in range(len(b)) if a[k] != b[k]]
                        if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in diff_chars):
                            continue
                        # Avoid common-word homographs such as 心脏/心里; pinyin pass covers name drift.
                        if a[-1] in _SEMANTIC_SUFFIXES or b[-1] in _SEMANTIC_SUFFIXES:
                            continue
                        candidates.append((a, b))
                elif len(a) >= 2 and len(b) >= 2:
                    # Different lengths: first 2 chars must match
                    if a[:2] == b[:2]:
                        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
                        extra_counts = collections.Counter(longer)
                        extra_counts.subtract(collections.Counter(shorter))
                        extra = ''.join(c for c, n in extra_counts.items() for _ in range(max(0, n)))
                        if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in extra):
                            continue
                        candidates.append((a, b))

    # Cross-group comparison: detect variants with different first chars
    # but matching suffixes (e.g., 约书亚/耶苏亚 share "书亚").
    # Group same-length tokens by suffix (chars[1:]), compare across groups.
    _MAX_SUFFIX_GROUP = 40
    if len(freq2) >= 2:
        suffix_groups = collections.defaultdict(list)
        for t in freq2:
            if len(t) >= 3:
                g = suffix_groups[t[1:]]
                if len(g) < _MAX_SUFFIX_GROUP:
                    g.append(t)
        for suffix, suffix_tokens in suffix_groups.items():
            if len(suffix_tokens) < 2:
                continue
            # Only compare tokens with different first chars
            for i in range(len(suffix_tokens)):
                a = suffix_tokens[i]
                for j in range(i + 1, len(suffix_tokens)):
                    b = suffix_tokens[j]
                    if a[0] == b[0]:
                        continue  # already compared in within-group pass
                    # Same length guaranteed (same suffix + both len>=3)
                    # Diff chars are positions 0 for both (their first chars)
                    diff_chars = [a[0], b[0]]
                    if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in diff_chars):
                        continue
                    # shared = len-1 (all suffix chars match)
                    candidates.append((a, b))

    # Pinyin comparison: catches same-sound transliteration drift with different chars
    # (e.g., 艾露亚/埃鲁) without doing broad fuzzy matching on ordinary words.
    _PINYIN_FALLBACK = {
        "阿": "a", "艾": "ai", "埃": "ai", "爱": "ai", "安": "an", "奥": "ao",
        "巴": "ba", "拜": "bai", "百": "bai", "贝": "bei", "冰": "bing", "伯": "bo", "波": "bo", "布": "bu",
        "卡": "ka", "凯": "kai", "科": "ke", "柯": "ke", "克": "ke", "库": "ku", "昆": "kun",
        "丹": "dan", "达": "da", "德": "de", "迪": "di", "蒂": "di", "顿": "dun", "敦": "dun",
        "尔": "er", "恩": "en", "法": "fa", "菲": "fei", "芬": "fen", "弗": "fu", "夫": "fu",
        "格": "ge", "哈": "ha", "赫": "he", "吉": "ji", "加": "jia", "迦": "jia", "嘉": "jia",
        "拉": "la", "兰": "lan", "岚": "lan", "莱": "lai", "来": "lai", "勒": "le", "雷": "lei", "蕾": "lei",
        "黎": "li", "利": "li", "里": "li", "莉": "li", "琳": "lin", "林": "lin", "伦": "lun", "罗": "luo", "洛": "luo",
        "卢": "lu", "鲁": "lu", "露": "lu", "璐": "lu",
        "玛": "ma", "马": "ma", "麦": "mai", "曼": "man", "蒙": "meng", "莫": "mo", "穆": "mu",
        "娜": "na", "纳": "na", "奈": "nai", "妮": "ni", "尼": "ni", "诺": "nuo",
        "帕": "pa", "佩": "pei", "珀": "po", "普": "pu", "琪": "qi", "齐": "qi", "琦": "qi",
        "瑞": "rui", "萨": "sa", "塞": "sai", "桑": "sang", "瑟": "se", "森": "sen", "沙": "sha", "舍": "she",
        "施": "shi", "士": "shi", "斯": "si", "索": "suo", "塔": "ta", "泰": "tai", "坦": "tan", "特": "te",
        "维": "wei", "威": "wei", "韦": "wei", "沃": "wo", "温": "wen", "希": "xi", "修": "xiu",
        "雅": "ya", "娅": "ya", "亚": "ya", "耶": "ye", "约": "yue", "扎": "zha", "朱": "zhu",
    }

    def _pinyin_syllables(token):
        try:
            from pypinyin import lazy_pinyin
            syllables = lazy_pinyin(token, errors="ignore")
            if len(syllables) == len(token):
                return tuple(s.lower() for s in syllables if s)
        except Exception:
            pass
        syllables = [_PINYIN_FALLBACK.get(ch) for ch in token]
        if any(s is None for s in syllables):
            return ()
        return tuple(syllables)

    def _is_translit_like(token):
        if token[-1] in _SEMANTIC_SUFFIXES and token[-1] not in _TRANSLITERATION_CHARS:
            return False
        hits = sum(1 for ch in token if ch in _TRANSLITERATION_CHARS)
        if hits >= max(2, math.ceil(len(token) * 0.6)):
            return True
        syllables = _pinyin_syllables(token)
        return len(syllables) >= 2 and all(s in {
            "a", "ai", "an", "ao", "ba", "bai", "bei", "bo", "bu", "ka", "kai", "ke", "ku", "kun",
            "da", "dan", "de", "di", "dun", "er", "en", "fa", "fei", "fen", "fu", "ge", "ha", "he",
            "ji", "jia", "la", "lan", "lai", "le", "lei", "li", "lin", "lu", "lun", "luo", "ma",
            "mai", "man", "meng", "mo", "mu", "na", "nai", "ni", "nuo", "pa", "pei", "po", "pu",
            "qi", "rui", "sa", "sai", "sang", "se", "sen", "sha", "she", "shi", "si", "suo", "ta",
            "tai", "tan", "te", "wei", "wo", "wen", "xi", "xiu", "ya", "ye", "yue", "zha", "zhu",
        } for s in syllables)

    def _pinyin_match(a_py, b_py):
        if len(a_py) < 2 or len(b_py) < 2 or a_py[0] != b_py[0]:
            return 0.0
        shorter, longer = (a_py, b_py) if len(a_py) <= len(b_py) else (b_py, a_py)
        shared_prefix = 0
        for x, y in zip(shorter, longer):
            if x != y:
                break
            shared_prefix += 1
        if a_py == b_py:
            return 1.0
        if shared_prefix >= 2 and tuple(longer[:len(shorter)]) == tuple(shorter) and len(longer) - len(shorter) <= 2:
            if len(shorter) == 2 and len(longer) == 3:
                return 0.8
            return shared_prefix / len(longer)
        return 0.0

    pinyin_by_first = collections.defaultdict(list)
    for token in freq2:
        if tokens[token] < 2:
            continue
        if not _is_translit_like(token):
            continue
        syllables = _pinyin_syllables(token)
        if len(syllables) >= 2:
            pinyin_by_first[syllables[0]].append((token, syllables))

    for group in pinyin_by_first.values():
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda item: (-len(item[1]), item[0]))[:_MAX_GROUP_SIZE]
        for i in range(len(group)):
            a, a_py = group[i]
            for j in range(i + 1, len(group)):
                b, b_py = group[j]
                if a == b:
                    continue
                match_score = _pinyin_match(a_py, b_py)
                if match_score <= 0:
                    continue
                if any(ch in _SEMANTIC_SUFFIXES and ch not in _TRANSLITERATION_CHARS for ch in a + b):
                    continue
                if set(a).isdisjoint(set(b)) and match_score < 0.8:
                    continue
                if len(a) == len(b) and sum(1 for ch in a if ch in b) < len(a) - 1:
                    continue
                key = tuple(sorted([a, b]))
                pinyin_pairs.add(key)
                pinyin_scores[key] = max(pinyin_scores.get(key, 0.0), match_score)
                candidates.append((a, b))

    seen = set()
    result = []
    for a, b in candidates:
        key = tuple(sorted([a, b]))
        if key not in seen:
            seen.add(key)
            # Frequency threshold: both tokens must appear ≥2 times.
            # Lowered from 3 to 2 to catch rare variants (e.g., a name
            # translated correctly 95% of the time but slipped 2-3 times).
            # (Former proper-prefix filter removed — it blocked genuine
            # name-suffix variants. The semantic suffix filter in the
            # candidate-formation loop already handles common suffixes.)
            if freq2.get(a, 0) < 2 or freq2.get(b, 0) < 2:
                continue
            shared = sum(1 for k in range(min(len(a), len(b))) if a[k] == b[k])
            max_len = max(len(a), len(b))
            freq = freq2.get(a, 0) + freq2.get(b, 0)
            score = (shared / max_len) * math.log(freq + 1)
            if key in pinyin_pairs and shared == 0:
                score = pinyin_scores.get(key, 0.6) * math.log(freq + 1) * 0.9
            group_key = a[0]  # first-char group
            result.append((a, b, freq, score, group_key))
    result.sort(key=lambda x: (-x[3], -x[2]))

    # Diversity: ensure small groups (2-10 members) get ≥1 entry each.
    # Build the top list first, then swap in diversity picks for the
    # lowest-scored entries so we don't lose total count.
    top = list(result)  # already sorted by score
    small_groups = {ch for ch, grp in by_first.items() if 2 <= len(grp) <= 10}
    groups_in_top = {item[4] for item in top[:top_n]}
    missing = small_groups - groups_in_top
    if missing:
        # Find best candidate per missing group
        group_best = {}
        for item in result:
            gk = item[4]
            if gk in missing and gk not in group_best:
                group_best[gk] = item
        # Replace the lowest-scored entries with diversity picks
        n_replace = min(len(missing), top_n // 4)  # at most 25% diversity
        if n_replace > 0:
            extras = sorted(group_best.values(), key=lambda x: -x[3])[:n_replace]
            top = top[:top_n - n_replace] + extras
            top.sort(key=lambda x: -x[3])

    return [(a, b, f) for a, b, f, *_ in top[:top_n]]


def _generate_voice_cards(work_dir):
    """Extract character dialogue samples for round 2 voice consistency check.

    Scans corrected (or preprocessed) text for dialogue attributed to named
    speakers, groups by character, and writes representative samples to
    voice_cards.md. The LLM uses these in round 2 to verify each character
    speaks with a consistent voice across all batches.
    """
    work_dir = Path(work_dir)
    extracted_dir = work_dir / "extracted"
    glossary = load_glossary(str(work_dir))

    # Collect all text, preferring corrected over preprocessed
    index_path = extracted_dir / "index.json"
    if not index_path.exists():
        return
    index = json.load(open(index_path, "r", encoding="utf-8"))

    all_text = ""
    for item in index:
        ch = item["chapter"]
        corr = extracted_dir / f"chapter_{ch:04d}_corrected.json"
        sentinel = extracted_dir / f"chapter_{ch:04d}.corrected"
        pp = extracted_dir / f"chapter_{ch:04d}_preprocessed.json"
        raw = extracted_dir / f"chapter_{ch:04d}.json"

        if corr.exists() and sentinel.exists():
            path = corr
        elif pp.exists():
            path = pp
        else:
            path = raw
        if path.exists():
            if all_text:
                all_text += "\n---\n"
            data = json.load(open(path, "r", encoding="utf-8"))
            for s in data.get("segments", []):
                all_text += s.get("content", "")

    # Find dialogue with speaker attribution.
    # Patterns: "NAME说", "NAME道", "NAME问", "NAME喊", "NAME回答",
    # "NAME开口", "NAME低语", "NAME轻声说", etc.
    speaker_pattern = re.compile(
        r'([\u4e00-\u9fff·]{2,6})(?:轻声|低声|小声|冷冷|淡淡|缓缓|轻轻|'
        r'微微|慢慢|忽然|突然|不禁|不由得|笑着|哭着|怒|叹|'
        r'说道|回答|开口|低语|告诉|问道|喊道|叫道|'
        r'说|道|问|喊|叫|答)'
    )
    quote_pattern = re.compile(r'[「『]([^」』]+)[」』]|"([^"]+)"|\u201c([^\u201d]+)\u201d')

    # Collect dialogue by speaker
    speakers = {}  # name -> [(dialogue_text, context_snippet)]
    pos = 0
    while pos < len(all_text):
        m = speaker_pattern.search(all_text, pos)
        if not m:
            break
        speaker = m.group(1)
        pos = m.end()

        # Look for quote after the attribution (within 50 chars)
        window = all_text[pos:pos + 50]
        qm = quote_pattern.search(window)
        if qm:
            dialogue = qm.group(1) or qm.group(2) or qm.group(3) or ""
            if len(dialogue) > 4:
                ctx = all_text[max(0, m.start() - 20):pos + qm.end() + 20]
                if speaker not in speakers:
                    speakers[speaker] = []
                speakers[speaker].append((dialogue, ctx))
                pos = pos + qm.end()
        else:
            pos = max(pos - len(speaker), m.start() + 1)

    # Filter: only keep speakers whose name appears in the glossary
    # (real character names) OR who have 15+ dialogue instances.
    # This removes false positives like "不知道"/"我也是" that happen
    # to match the speaker attribution regex but aren't actual characters.
    known_names = set(glossary.keys()) | set(glossary.values())
    major = {}
    for name, samples in speakers.items():
        if len(samples) < 3:
            continue
        if name in known_names:
            major[name] = samples
        elif len(samples) >= 8 and name[0] not in "我你他她它这那":
            major[name] = samples  # frequent enough, likely real character

    if not major:
        return

    # Pick 3-5 diverse samples per character (short/medium/long)
    out_path = work_dir / "voice_cards.md"
    lines = [
        "## 角色声调卡",
        "",
        "以下为第 1 轮校对后从全文中提取的主要角色对话样本。",
        "第 2 轮精修时，每次读到该角色的对话，**对照声调卡验证**：",
        "- 说话风格是否一致（文雅/粗鄙/简洁/啰嗦）",
        "- 语气是否与该角色身份、性格匹配",
        '- 同一个人是否在不同 batch 里"换了声音"',
        "",
    ]
    for name, samples in sorted(major.items(), key=lambda x: -len(x[1])):
        # Pick diverse samples
        sorted_samples = sorted(samples, key=lambda x: len(x[0]))
        # Pick 5-7 diverse samples: shortest, longest, and 3 quartile points
        picks = [sorted_samples[0]]
        if len(sorted_samples) > 1:
            picks.append(sorted_samples[-1])
        n = len(sorted_samples)
        for frac in (n // 4, n // 2, 3 * n // 4):
            if 0 < frac < n - 1:
                candidate = sorted_samples[frac]
                if candidate not in picks:
                    picks.append(candidate)
        # Fill remaining slots from middle if available
        if len(picks) < 5 and len(sorted_samples) > len(picks):
            step = max(1, len(sorted_samples) // 6)
            for i in range(step, len(sorted_samples) - 1, step):
                if len(picks) >= 7:
                    break
                if sorted_samples[i] not in picks:
                    picks.append(sorted_samples[i])

        lines.append(f"### {name}（{len(samples)} 处对话）")
        lines.append("")
        for i, (dialogue, ctx) in enumerate(picks[:7]):
            lines.append(f"**样本 {i + 1}**：")
            lines.append(f"> {dialogue}")
            lines.append(f"上下文：…{ctx}…")
            lines.append("")
        lines.append("---")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  Voice cards: {len(major)} characters → {out_path.relative_to(work_dir.parent)}")


def _auto_generate_corrections(work_dir):
    """Generate corrections_auto.json with mechanical fixes.

    Pre-fills blacklist word replacements and English-with-Chinese-pair
    deletions. The LLM only needs to add glossary_additions and handle
    untranslated English segments. Saves significant LLM effort.
    """
    work_dir = Path(work_dir)
    extracted_dir = work_dir / "extracted"
    config = load_config(work_dir)
    blacklist = config.get("blacklist", [])

    # Patterns for mechanical fixes
    # Fix 1: Block-level CJK duplication (e.g. "海辛瑟海辛瑟" → "海辛瑟").
    # Uses {3,4} (not {2,4}) to avoid matching legitimate ABAB verb
    # reduplication like "考虑考虑"/"商量商量" (2-char reduplication is
    # standard Chinese grammar, not a translation artifact).
    _dup_re = re.compile(r'([\u4e00-\u9fff]{3,4})\1')
    _tn_re = re.compile(r'[（(]注[：:][^）)]{1,300}[）)]')

    corrections = []
    for fpath in sorted(extracted_dir.glob("chapter_*_preprocessed.json")):
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        ch = data["chapter"]
        segs = data.get("segments", [])
        for i, s in enumerate(segs):
            c = s.get("content", "")
            sid, sub = s["id"], s.get("sub_id", 0)
            nc = c

            # Fix 1: Duplicate CJK blocks ("海海辛瑟" → "海辛瑟")
            # Only matches 2-4 char block repeats, not single-char reduplication ("哈哈")
            nc = _dup_re.sub(r'\1', nc)

            # Fix 2: Translator notes ("（注：...）" → delete)
            nc_after_tn = _tn_re.sub('', nc)
            if nc_after_tn != nc:
                nc = nc_after_tn.strip() if nc_after_tn.strip() else " "

            # Fix 3: Blacklist default replacements.
            # Use CJK boundary regex (same as preprocessor) to avoid corrupting
            # legitimate words that contain the blacklist term as substring
            # (e.g. "情不自禁" must not become "情不由禁").
            bl_defaults = config.get("blacklist_defaults", {})
            if s.get("blacklisted"):
                for h in s.get("blacklist_hits", []):
                    default = bl_defaults.get(h, h)
                    if default != h:
                        escaped = re.escape(h)
                        nc = re.sub(
                            r'(?<![\u3400-\u4dbf\u4e00-\u9fff])' + escaped + r'(?![\u3400-\u4dbf\u4e00-\u9fff])',
                            default, nc
                        )

            # Fix 4: English with nearby Chinese → auto-delete
            if s.get("is_english"):
                has_cn = False
                for j in range(max(0, i - 5), min(len(segs), i + 6)):
                    if j == i:
                        continue
                    n = segs[j].get("content", "")
                    cj = sum(1 for x in n if '\u4e00' <= x <= '\u9fff')
                    if cj < 20:
                        continue
                    if sum(1 for x in n if x.isascii() and x.isalpha()) / max(cj, 1) < 0.2:
                        has_cn = True
                        break
                if has_cn:
                    # Guard: English with 3+ lines (2+ line breaks) is likely
                    # deliberate literary content (poems, letters, spells).
                    # Single newlines may be HTML formatting artifacts in
                    # translation residue; only protect multi-line structure.
                    if len(c.splitlines()) < 3:
                        nc = " "

            if nc != c:
                corrections.append({
                    "chapter": ch, "segment_id": f"{sid}.{sub}",
                    "corrected": nc
                })

    out_path = work_dir / "corrections_auto.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"glossary_additions": [], "corrections": corrections},
                  f, ensure_ascii=False, indent=2)
    # Count
    del_cnt = sum(1 for c in corrections if c["corrected"] in (" ", ""))
    print(f"  Auto-corrections: {len(corrections)} ({del_cnt} deletions, "
          f"{len(corrections) - del_cnt} blacklist/dup/note replacements)")
    print(f"  Saved to: {out_path.relative_to(work_dir.parent)}")


def _cleanup_residual_english(work_dir):
    """Pre-inject safety net: remove English segments the LLM missed.

    Scans _corrected.json for segments where is_english=True and content
    matches _preprocessed.json (LLM never touched them). For those with
    nearby Chinese translations: auto-delete. For those without: flag.

    Returns (deleted, flagged) counts.
    """
    work_dir = Path(work_dir)
    extracted_dir = work_dir / "extracted"
    index_path = extracted_dir / "index.json"
    if not index_path.exists():
        return 0, 0

    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    missed_deleted = 0
    missed_flagged = 0

    for item in index:
        ch = item["chapter"]
        corr_path = extracted_dir / f"chapter_{ch:04d}_corrected.json"
        sentinel = extracted_dir / f"chapter_{ch:04d}.corrected"
        pp_path = extracted_dir / f"chapter_{ch:04d}_preprocessed.json"

        if not corr_path.exists() or not sentinel.exists():
            continue
        if not pp_path.exists():
            continue

        with open(corr_path, "r", encoding="utf-8") as f:
            corr_data = json.load(f)
        with open(pp_path, "r", encoding="utf-8") as f:
            pp_data = json.load(f)

        pp_by_key = {}
        for s in pp_data.get("segments", []):
            key = (s["id"], s.get("sub_id", 0))
            pp_by_key[key] = s

        segs = corr_data.get("segments", [])
        modified = False

        for i, s in enumerate(segs):
            if not s.get("is_english"):
                continue
            key = (s["id"], s.get("sub_id", 0))
            pp_seg = pp_by_key.get(key)
            if not pp_seg:
                continue

            corr_content = s.get("content", "")
            pp_content = pp_seg.get("content", "")

            # LLM touched this segment → skip
            if corr_content != pp_content:
                continue

            # Still English? Re-run the same detection as cmd_preprocess
            still_english = _is_english_heavy(corr_content)
            if not still_english:
                continue

            # LLM missed this English segment. Check for CN neighbors.
            has_cn = False
            for j in range(max(0, i - 5), min(len(segs), i + 6)):
                if j == i:
                    continue
                n = segs[j].get("content", "")
                cj = sum(1 for x in n if '\u4e00' <= x <= '\u9fff')
                if cj < 20:
                    continue
                if sum(1 for x in n if x.isascii() and x.isalpha()) / max(cj, 1) < 0.2:
                    has_cn = True
                    break

            if has_cn:
                # Guard: skip literary English (3+ lines = deliberate structure)
                if len(corr_content.splitlines()) < 3:
                    s["content"] = " "
                    missed_deleted += 1
                    modified = True
                else:
                    missed_flagged += 1
            else:
                missed_flagged += 1

        if modified:
            tmp_path = extracted_dir / f"chapter_{ch:04d}_corrected.json.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(corr_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, corr_path)

    if missed_deleted:
        print(f"  Residual EN cleanup: {missed_deleted} missed English segments "
              f"auto-deleted (CN pair found)")
    if missed_flagged:
        print(f"  ⚠ {missed_flagged} English segments untranslated "
              f"(no CN pair) — may appear in EPUB")

    return missed_deleted, missed_flagged


def _write_task_md(work_dir):
    """Generate TASK.md with ready-to-use Claude prompt and workflow."""
    work_dir = Path(work_dir)
    batch_dir = work_dir / "proofread_batches"
    full_text = work_dir / "full_text.txt"
    has_batches = batch_dir.exists() and list(batch_dir.glob("batch_*.txt"))

    # Check if clean-batches mode is active
    config = load_config(work_dir)
    clean_batches = config.get("proofreading", {}).get("clean_batches", False)
    has_checklists = clean_batches and has_batches and list(batch_dir.glob("batch_*_checklist.json"))

    # Generate suspected term variant hints (50 to include lower-freq pairs)
    extracted_dir = work_dir / "extracted"
    variants = _find_suspected_variants(extracted_dir, top_n=50)
    english_terms = _find_english_terms(extracted_dir)

    if clean_batches:
        lines = [
            "## EPUB 中文校对任务",
            "",
            "你现在是一个中文出版级校对员。校对分两轮进行。",
            "",
            "**本任务使用 clean batch 模式：正文不含任何 `[? ...]` 标记。**",
            "标记已剥离到各 batch 对应的 `_checklist.json` 文件中。",
            "",
            "### 处理流程（两文件工作流）",
            "",
            "每个 batch 有两个文件：",
            "- `batch_NN_*.txt` — **纯正文**，仅含坐标 `[cN.sM]`，无任何标记",
            "- `batch_NN_*_checklist.json` — **标记清单**，按 segment_id 索引",
            "",
            "**处理顺序（防标记 tunnel-vision）：**",
            "1. 打开 .txt 文件 → 逐段通读（正文无标记，只能主动扫描）→ 产出 corrections",
            "2. 打开 _checklist.json → 逐条对照：自己漏了哪些？哪些是误报？→ 补充 corrections",
            "3. 输出自检报告 + 运行 apply-corrections",
            "",
            "### 强制执行规则",
            "",
            "**以下规则不可跳过、不可缩短：**",
            "",
            "1. **每个 batch 必须完整、逐段深入阅读。** 覆盖该 batch 的全部内容，不可抽样。",
            "2. **正文中无标记，必须主动逐段扫描。** 不能等标记提示——标记在 checklist 中，",
            "   只有完成主动扫描后才能打开对照。如果发现自己的分析被标记覆盖了则确认，",
            "   标记指出但自己遗漏的则补充，标记误报的则拒绝。",
            "3. **禁止跳过 batch。** 全书所有 batch 都必须逐个处理。",
            "4. **每个 batch 处理完必须立即 apply-corrections。** 后面的 batch",
            "   可能发现前面遗漏的术语变体；apply 时 reprocess 会自动传播。",
            "5. **阅读时注意以下所有问题：**",
            "- 同一个外文人名/地名/神名的不同中文翻译",
            "- 未翻译的英文单词",
            "- 网文词需替换为中性表达（对照 checklist 中的 \"网文词\" 字段）",
            "- 英文段处理（对照 checklist 中的 \"英文\" 字段：\"删除\"=旁有中文可删，\"翻译\"=无中文需译）",
            "- AI 套话、翻译腔、风格突变",
            "- 翻译完整性：对话引导句是否遗漏状态修饰",
            "- 格言/重复句式：全书反复出现的固定短语是否逐字一致",
            "",
            "### 第 1 轮 — 术语发现 + 黑名单",
            "逐 batch 深度阅读，重点搜集**同指异译**的人名/地名/神名，写入 glossary_additions。",
            "每 batch 的 apply-corrections 会自动 reprocess，把新术语传播到全量章节。",
            "",
            "### 第 2 轮 — 英文处理 + 精修",
            "处理英文段、AI 套话、翻译腔、风格突变。对照 checklist 中的 \"英文\" 和 \"审视词\" 字段。",
            "",
            "**重要：不要在所有 batch 处理完之前运行 inject/pack。**",
            "",
            "---",
            "",
            "### 第 1 轮：逐 batch 校对",
        ]
    else:
        lines = [
            "## EPUB 中文校对任务",
            "",
            "你现在是一个中文出版级校对员。校对分两轮进行。",
            "",
            "### 强制执行规则",
            "",
            "**以下规则不可跳过、不可缩短、不可「快速扫描」：**",
            "",
            "1. **每个 batch 必须完整、逐段深入阅读。** 覆盖该 batch 的全部内容，",
            "   不可只读开头几段或抽样。无论 batch 大小，必须读完。",
            "2. **认真审读每一段文本。** 不要只扫描 `[? ...]` 标记。许多术语",
            "   变体和翻译腔不会自动标记，需要人工逐段发现。",
            "3. **禁止跳过 batch。** 全书所有 batch 都必须逐个处理，不得以",
            "   「术语已经够多」为由跳过后面的 batch。",
            "4. **每个 batch 处理完必须立即 apply-corrections。** 后面的 batch",
            "   可能发现前面遗漏的术语变体；apply 时 reprocess 会自动把新术语",
            "   传播到全量章节（包括已校对过的章节的 `_corrected.json`）。",
            "5. **阅读时注意以下所有问题，不要遗漏：**",
            "- 同一个外文人名/地名/神名的不同中文翻译（如「德拉奈」/「德洛内」应为统一的「德劳内」）",
            "- 未翻译的英文单词（如 anguissette 应为「痛苦者」）",
            "- `[? 需替换网文词: xxx]` 标记 → 替换为中性表达",
            "- `[? 英文段落]` → 旁有中文译文则删除英文段",
            "- `[? 英文段落·待翻译]` → 翻译为中文",
            "- AI 套话（'在上一章中'、'综上所述'等）、翻译腔、风格突变",
            "- **翻译完整性**：对话引导句（「他说，笑着」）是否遗漏了状态修饰（漏了「笑着」），常见省略如笑着说、叹了口气、低声、冷冷地等",
            "- **格言/重复句式统一**：全书中反复出现的格言、座右铭、仪式用语必须在所有出现处逐字一致。处理每个 batch 时注意是否有前文出现过的固定短语在本 batch 以不同措辞出现",
            "",
            "### 第 1 轮 — 术语发现 + 黑名单",
            "逐 batch 深度阅读，重点搜集**同指异译**的人名/地名/神名，写入 glossary_additions。",
            "同时注意全书中反复出现的**格言、座右铭、仪式用语**是否在所有出现处逐字一致。",
            "每 batch 的 apply-corrections 会自动 reprocess，把新术语传播到全量章节。",
            "",
            "### 第 2 轮 — 英文处理 + 精修",
            "处理 `[? 英文段落]` 标记、AI 套话、翻译腔、风格突变。",
            "",
            "**重要：不要在所有 batch 处理完之前运行 inject/pack。**",
            "",
            "---",
            "",
            "### 第 1 轮：逐 batch 校对",
        ]

    if has_batches:
        batches = sorted(batch_dir.glob("batch_*.txt"))
        lines.append("")
        lines.append(f"全书共 {len(batches)} 个 batch。对每个 batch 执行：")
        lines.append("")
        if clean_batches:
            lines.append("a) **先读取纯正文** `batch_NN_*.txt`——逐段主动扫描（正文无标记，不能等提示）")
            lines.append("b) 产出第一批 corrections（基于自己的分析）")
            lines.append("c) **再打开** `batch_NN_*_checklist.json`——对照每一条标记：")
            lines.append("   - 自己已发现的 → 确认")
            lines.append("   - 自己遗漏的 → 补充到 corrections")
            lines.append("   - 标记误报的 → 拒绝")
            lines.append("d) 输出自检报告（X/Y/Z/W），然后输出合并后的 corrections.json")
        else:
            lines.append("a) 读取 batch 文件")
            lines.append("b) 审读全文，重点找术语变体和黑名单标记")
            lines.append("c) 输出 glossary_additions + corrections 到 corrections.json")
        lines.append(f"d) 运行 `python proofread.py apply-corrections {work_dir} corrections.json`")
        lines.append("e) 确认 apply 成功后，继续下一个 batch")
        lines.append("")
        lines.append("**Batch 清单：**")
        for i, b in enumerate(batches):
            line = f"  {i+1}. `{b.relative_to(work_dir)}`"
            if clean_batches:
                cpath = Path(str(b).replace(".txt", "_checklist.json"))
                if cpath.exists():
                    line += f" + `{cpath.relative_to(work_dir)}`"
            lines.append(line)
        lines.append("")
        lines.append("处理完最后一个 batch 后，进入第 2 轮。")
    else:
        lines.append("")
        lines.append("全书未分卷，只有单个文件。")
        lines.append(f"- 读取 `{full_text.relative_to(work_dir)}`  → 输出 glossary_additions + corrections")
        lines.append(f"- 运行 `python proofread.py apply-corrections {work_dir} corrections.json`")
        lines.append("- apply 成功后，进入第 2 轮")

    # Suspected variant hints
    if variants:
        lines.extend([
            "",
            "### 疑似术语变体（请逐对确认）",
            "",
            "以下词对经算法筛选，可能是同一外文名的不同中译。**第 1 轮阅读时逐对确认**：",
            "- 确认是同一专名的不同译法 → 写入 glossary_additions",
            '- 是不同概念（如"斯卡迪人"和"斯卡迪语"）→ 跳过',
            "",
        ])
        for a, b, freq in variants[:30]:
            lines.append(f"- `{a}` ↔ `{b}`（共现 {freq} 次）")
        lines.append("")
        lines.append("**用法**：在 batch 中确认它们是同一外文名的不同中译后，写入 glossary_additions。")
        lines.append("不确定的不要加。只统一「明显是同一专名」的情况。")

    # Untranslated English terms (auto-detected from preprocessed text)
    if english_terms:
        lines.extend([
            "",
            "### 疑似未翻译英文术语（出现≥3次的英文单词，应统一翻译为中文）",
            "",
            "以下英文单词在全书高频出现，很可能是未翻译的专有术语。",
            "请在 batch 中确认其含义后，写入 glossary_additions（例如 `anguissette`→`痛苦者`）：",
            "",
        ])
        for word, freq in english_terms:
            lines.append(f"- `{word}`（出现 {freq} 次）")
        lines.append("")
        lines.append("**用法**：确认为专有术语后，在 glossary_additions 中添加翻译映射。")
        lines.append("常见英文单词（the/and/he/she 等）可忽略。pack 时会自动应用 glossary 到 EPUB。")

    # Cross-script bridging: connect English terms to Chinese variant pairs
    if variants and english_terms:
        lines.extend([
            "",
            "### 跨文字桥接（英文术语 ↔ 中文异译）",
            "",
            "**注意**：上方两个列表可能存在对应关系。同一外文专名的**英文原词**和**中文异译**应一并处理。",
            "请在确认术语时对照两组列表：",
            "- 如果英文术语（如 `hyacinthe`）与某组中文变体（如 海辛瑟/希亚辛特）指向同一外文名 → 统一中文译名，并在 glossary_additions 中添加英文→中文映射",
            "- 如果英文术语在中文变体列表中**没有**对应 → 该外文名可能尚未翻译，需确认后写入 glossary_additions",
            "- 如果中文变体在英文术语列表中**没有**对应原词 → 该外文名可能来自非英文源语言，或英文原词被其他词取代",
            "",
            "**示例确认流程**：",
            "1. 找到英文术语 `trevalion` → 在中变体列表搜索「特雷瓦」→ 确认对应关系",
            "2. 写入 `glossary_additions`：`trevalion`→`特雷瓦利翁`（统一译名）",
            "3. 中文异译（特雷瓦利昂/特里瓦利翁）自动归并为统一译名",
            "",
        ])

    if clean_batches:
        lines.extend([
            "",
            "---",
            "",
            "### 第 2 轮：英文处理 + 精修",
            "",
            "第 1 轮全部 batch apply 完毕后，`voice_cards.md` 已自动生成",
            "（voice_cards 供可选的第 3 轮文学润色使用，第 2 轮无需对照）。",
            "现在从第 1 个 batch 重新开始。**注意：reprocess 后 checklist 可能已更新。**",
            "",
            "a) 读取纯正文 `batch_NN_*.txt`（此时术语应已统一，网文词标记应消失）",
            "b) 逐段主动扫描翻译腔、AI套话、风格突变",
            "c) 打开 `batch_NN_*_checklist.json`——对照 checklist 中的残留标记：",
            "   - \"英文\": \"删除\" → pipeline 已自动处理，跳过",
            "   - \"英文\": \"翻译\" → 翻译为中文并写入 corrections",
            "   - \"审视词\" → 逐词判断是否需替换",
            "d) 修正 AI 套话、翻译腔、风格突变。逐段对照以下翻译腔模式：",
            "   - 「被……所……」→ 改主动（如「他被命运所抛弃」→「他遭命运抛弃」）",
            "   - 「是……的」→ 去掉冗余（如「这本书是值得一读的」→「这本书值得一读」）",
            "   - 「一个……的」→ 合并形容词（如「一个黑暗的、潮湿的夜晚」→「一个潮湿黑暗的夜晚」）",
            "   - 「……着……着」→ 简化（如「他走着走着」→「他走了一阵」）",
            "   - 「开始……起来」→ 换动词（如「他开始跑起来」→「他拔腿就跑」）",
            "   - 过于正式的代词 → 「该」→「这个/那」、「该事件」→「这件事」",
            "   - 被动语态过滥 → 「被/让人们/被人们」→ 主动语态或删除施动者",
            "   - 注：只改明显翻译腔，不确定的保留。不要强行改写正常中文",
            "   - **翻译完整性检查**：对话引导句中检查是否遗漏状态修饰（笑着说、叹了口气等）",
            "e) **边界平滑检查**：每个 batch 开头有 `[BOUNDARY CHECK]` 标记",
            "   - 对比标记前后 3 段的语气、节奏、用词",
            "   - 若上一 batch 结尾和本 batch 开头风格不衔接 → 写入 corrections",
            "f) 输出自检报告 + corrections（本轮通常无 glossary_additions）",
            f"g) 运行 `python proofread.py apply-corrections {work_dir} corrections.json`",
            "h) 继续下一个 batch",
            "",
        ])
    else:
        lines.extend([
            "",
            "---",
            "",
            "### 第 2 轮：英文处理 + 精修",
            "",
            "第 1 轮全部 batch apply 完毕后，`voice_cards.md`（主要角色对话样本）已自动生成",
            "（voice_cards 供可选的第 3 轮文学润色使用，第 2 轮无需对照）。",
            "现在从第 1 个 batch 重新开始：",
            "",
            "a) 读取 batch 文件（此时 `[? 需替换网文词: xxx]` 应已消失，术语应已统一）",
            "b) 处理英文标记：",
            "   - `[? 英文段落]` → pipeline 已自动删除，**跳过，无需处理**",
            "   - `[? 英文段落·待翻译]` → 无中文译文，**翻译为中文并写入 corrections**",
            "c) 修正 AI 套话、翻译腔、风格突变。逐段对照以下翻译腔模式：",
            "   - 「被……所……」→ 改主动（如「他被命运所抛弃」→「他遭命运抛弃」）",
            "   - 「是……的」→ 去掉冗余（如「这本书是值得一读的」→「这本书值得一读」）",
            "   - 「一个……的」→ 合并形容词（如「一个黑暗的、潮湿的夜晚」→「一个潮湿黑暗的夜晚」）",
            "   - 「……着……着」→ 简化（如「他走着走着」→「他走了一阵」）",
            "   - 「开始……起来」→ 换动词（如「他开始跑起来」→「他拔腿就跑」）",
            "   - 过于正式的代词 → 「该」→「这个/那」、「该事件」→「这件事」",
            "   - 被动语态过滥 → 「被/让人们/被人们」→ 主动语态或删除施动者",
            "   - 注：只改明显翻译腔，不确定的保留。不要强行改写正常中文",
            "   - **翻译完整性检查**：对话/动作引导句中，检查中文是否遗漏了原文的状态修饰——",
            "     「他说，笑着」不应只译「他说」（漏了「笑着」），「她叹了口气回答」不应只译「她回答」。",
            "     常见易漏词：笑着说、叹了口气、低声、冷冷地、轻声、喃喃、眯起眼、点了点头等。",
            "     对照上下文判断——如果引导句异常简短，且上下文也**没有**通过其他句子传达该情绪/动作，",
            "     很可能是 AI 翻译时省略了引导句中的修饰，需要补回。如果上下文已传达了同样的信息则无需修改。",
            "d) **边界平滑检查**：每个 batch 开头有 `[BOUNDARY CHECK]` 标记",
            "   - 对比标记前后 3 段的语气、节奏、用词",
            "   - 若上一 batch 结尾和本 batch 开头风格不衔接 → 写入 corrections",
            "e) 输出 corrections（本轮通常无 glossary_additions）",
            f"f) 运行 `python proofread.py apply-corrections {work_dir} corrections.json`",
            "g) 继续下一个 batch",
            "",
        ])
    if has_batches:
        lines.append("**Batch 清单：**")
        for i, b in enumerate(batches):
            line = f"  {i+1}. `{b.relative_to(work_dir)}`"
            if clean_batches:
                cpath = Path(str(b).replace(".txt", "_checklist.json"))
                if cpath.exists():
                    line += f" + `{cpath.relative_to(work_dir)}`"
            lines.append(line)

    lines.extend([
        "",
        "---",
        "",
        "### 校对输出格式",
        "",
        "```json",
        '{"glossary_additions": [{"term": "异译名", "translation": "统一译名"}]}',
        "```",
        "",
        "```json",
        '{"corrections": [{"chapter": 0, "segment_id": 3, "corrected": "修正后的文本"}]}',
        "```",
        "",
        "### 规则",
        "",
        "- `segment_id` 用 `[cN.sM]` 或 `[cN.sM.K]` 坐标，写整数不要写 3.0",
        "- `[? 需替换网文词: xxx]` — **必须替换**为中性表达",
        "- `[? 英文段落]` — pipeline 已自动删除，无需处理",
        "- `[? 英文段落·待翻译]` — 英文段无中文配对→**翻译为中文**",
        "- **全中文化**：所有外文单词（含虚构术语如 anguissette/vrajna）必须翻译为中文，写入 glossary_additions",
        "- 术语统一只针对**同一外文名不同中译**，不把简称替换成全名",
        "- 引号内对话不改变原意，只修正错别字和语病",
        "- 删除 AI 套话（\"在上一章中\"\"综上所述\"等过渡性废话）",
        "- 相邻段落风格突变须平滑衔接，保留原文换段和标点风格",
        "- **概念同义词统一**：同指异名不限于专有名词。检查是否有不同词指同一概念",
        "  （如\"圣殿骑士\"↔\"圣堂武士\"、\"法师\"↔\"魔法师\"、\"王国\"↔\"帝国\"），",
        "  确认后写入 glossary_additions",
        "- **代词一致性**：同一角色代词不应漂移。如果某角色在 batch A 为\"他\"、",
        "  batch B 为\"她\"，必有一处错误，需回查原文修正",
        "- **角色声音一致性**：同一角色的对话风格应在全书一致。如果一个角色在",
        "  batch A 说话文雅、batch B 说话粗鄙，可能是 AI 分块翻译的拼接痕迹，",
        "  须调整为统一口吻",
        "",
        "### 全部完成后：检查 + 注入 + 打包",
        "",
        f"1. `python proofread.py check --diff {work_dir}`",
        f"2. `python proofread.py inject {work_dir}`",
        f"3. `python proofread.py pack {work_dir}`",
        "",
        "check 的非零退出通常是英文删除的 change ratio 虚高，diff 报告的修改段数吻合即可继续。",
        "pack 完成后告知用户输出 EPUB 路径。**不要删 work 目录**。",
        "如需检查修订细节（会剧透）：",
        f"- `python proofread.py check --diff-log diff.txt {work_dir}`",
        "",
        "### 全部完成后：输出无剧透统计报告",
        "",
        "pack 完成后，必须自动输出一份校对统计报告。",
        "报告只含数字和类别名称，**严禁包含任何剧情内容、角色命运、具体段落文本**。",
        "格式示例：",
        "",
        "```",
        "## 校对完成报告",
        "",
        "| 项目 | 数值 |",
        "|------|------|",
        "| 术语总条数 | xxx |",
        "| 黑名单词替换 | xxx 手动 + xxx 自动 |",
        "| 英文段落删除 | xxx |",
        "| 英文段落翻译 | xx |",
        "| 总修改段数 | xxx |",
        "",
        "输出：{path}/output.epub",
        "```",
    ])

    task_path = work_dir / "TASK.md"
    with open(task_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  Task file: {task_path}")


def cmd_pipeline(args):
    """Full pipeline: init → extract → [manual proofread] → inject → pack."""
    # Step 1: Init
    # Let cmd_init auto-derive work_dir from project directory (next to EPUB).
    # Only pass work_dir if user explicitly specified it.
    print("[1/5] Initializing work directory...")
    fake_args = argparse.Namespace(
        input_epub=args.input_epub,
        work_dir=args.work_dir,  # None → cmd_init uses project_dir/work/
        glossary=args.glossary,
        config=args.config,
        profile=getattr(args, 'profile', 'fantasy'),
        blacklist_file=getattr(args, 'blacklist_file', None)
    )
    ret = cmd_init(fake_args)
    if ret:
        return ret

    # Read actual work_dir: prioritize user-specified --work-dir, then
    # check context.json in default project directory (Claude Code workspace).
    input_path = Path(args.input_epub).resolve()
    if args.work_dir and Path(args.work_dir).exists():
        work_dir = Path(args.work_dir).resolve()
    else:
        project_dir = Path.cwd() / "proofread" / input_path.stem
        ctx_path = project_dir / "work" / "context.json"
        if ctx_path.exists():
            with open(ctx_path, "r", encoding="utf-8") as f:
                ctx = json.load(f)
            work_dir = Path(ctx.get("project_dir", "")) / "work"
        else:
            work_dir = project_dir / "work"

    # Step 2: Extract
    print("\n[2/5] Extracting text segments...")
    fake_args = argparse.Namespace(work_dir=str(work_dir))
    ret = cmd_extract(fake_args)
    if ret:
        return ret

    # Step 3: Preprocess (mechanical phases A+B)
    print("\n[3/5] Preprocessing (glossary replacement + blacklist detection)...")
    fake_args = argparse.Namespace(work_dir=str(work_dir))
    ret = cmd_preprocess(fake_args)
    if ret:
        return ret

    # Step 4: Dump text + generate task for Claude (fully automated)
    print("\n[4/5] Dumping text + generating task file...")
    max_chars = getattr(args, 'max_chars', 0) or 100000  # --max-chars flag or default
    clean_batches = getattr(args, 'clean_batches', False)
    # Persist in config so _redump_batches uses the same values
    config = load_config(work_dir)
    config.setdefault("proofreading", {})["max_chars"] = max_chars
    config["proofreading"]["clean_batches"] = clean_batches
    save_config(config, work_dir)
    dump_args = argparse.Namespace(work_dir=str(work_dir), max_chars=max_chars,
                                   clean_batches=clean_batches)
    ret = cmd_dump_text(dump_args)
    if ret:
        return ret

    # Write TASK.md — ready-to-use Claude prompt
    _write_task_md(work_dir)

    # Auto-generate corrections file with mechanical fixes
    _auto_generate_corrections(work_dir)

    # Auto-apply mechanical fixes (blacklist words + English deletions)
    # so the user doesn't need to remember to do it manually.
    auto_corr_path = work_dir / "corrections_auto.json"
    if auto_corr_path.exists():
        print("\n[5/5] Auto-applying mechanical fixes...")
        fake_args = argparse.Namespace(work_dir=str(work_dir), corrections_json=str(auto_corr_path))
        ret = cmd_apply_corrections(fake_args)
        if ret:
            print("  Warning: Some auto-corrections could not be applied.")
        print()

    # Generate character voice cards for round 2 consistency checks
    _generate_voice_cards(work_dir)

    print("\n" + "=" * 60)
    print("READY. 下一步：对 Claude 说「读取 TASK.md 并按指示操作」")
    print("=" * 60)
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    # On Windows with non-UTF-8 consoles (CP936/GBK), printing rare CJK
    # characters or emoji from LLM output causes UnicodeEncodeError crash.
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="EPUB Chinese Proofreading Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Initialize work directory from EPUB")
    p_init.add_argument("input_epub", help="Path to input EPUB")
    p_init.add_argument("work_dir", nargs="?", help="Work directory (default: {novel}/work/)")
    p_init.add_argument("--glossary", help="Path to external glossary JSON")
    p_init.add_argument("--config", help="Path to config JSON")
    p_init.add_argument("--profile", choices=BLACKLIST_PROFILES, default="fantasy",
                        help="Blacklist profile (fantasy|romance|general|minimal)")
    p_init.add_argument("--blacklist-file", help="Path to custom blacklist (.txt or .json)")

    p_extract = sub.add_parser("extract", help="Extract text segments from XHTML")
    p_extract.add_argument("work_dir", help="Work directory (from init)")

    p_preproc = sub.add_parser("preprocess", help="Run mechanical proofreading phases (A: glossary, B: blacklist)")
    p_preproc.add_argument("work_dir", help="Work directory")

    p_reproc = sub.add_parser("reprocess", help="Re-run preprocess with updated glossary (second pass)")
    p_reproc.add_argument("work_dir", help="Work directory")

    p_inject = sub.add_parser("inject", help="Apply proofread text back to XHTML")
    p_inject.add_argument("work_dir", help="Work directory")

    p_pack = sub.add_parser("pack", help="Repack EPUB from work directory")
    p_pack.add_argument("work_dir", help="Work directory")
    p_pack.add_argument("output_epub", nargs="?", help="Output EPUB path (default: auto from project dir)")

    p_pipe = sub.add_parser("pipeline", help="Run full pipeline (init+extract+preprocess)")
    p_pipe.add_argument("input_epub", help="Path to input EPUB")
    p_pipe.add_argument("--glossary", help="Path to external glossary JSON")
    p_pipe.add_argument("--config", help="Path to config JSON")
    p_pipe.add_argument("--profile", choices=BLACKLIST_PROFILES, default="fantasy",
                        help="Blacklist profile (fantasy|romance|general|minimal)")
    p_pipe.add_argument("--blacklist-file", help="Path to custom blacklist (.txt or .json)")
    p_pipe.add_argument("--work-dir", help="Work directory (default: auto-generated)")
    p_pipe.add_argument("--max-chars", type=int, default=0,
                        help="Auto-split into batches if total exceeds N chars. "
                             "Use 50000 for small-context models, 200000 for 1M+ models.")
    p_pipe.add_argument("--clean-batches", action="store_true",
                        help="Strip [? ...] markers from batch text; write separate "
                             "*_checklist.json files. Prevents LLM marker tunnel-vision.")

    p_addterm = sub.add_parser("add-term", help="Add/update a term in glossary")
    p_addterm.add_argument("work_dir", help="Work directory containing glossary.json")
    p_addterm.add_argument("term", help="Original term")
    p_addterm.add_argument("translation", help="Unified translation")

    p_addterms = sub.add_parser("add-terms", help="Batch add terms from JSON string")
    p_addterms.add_argument("work_dir", help="Work directory")
    p_addterms.add_argument("terms_json", help="JSON string: [{\"term\":\"x\",\"translation\":\"y\"},...]")

    p_check = sub.add_parser("check", help="Run mechanical checks on proofread output")
    p_check.add_argument("work_dir", help="Work directory")
    p_check.add_argument("chapter", nargs="?", help="Chapter number (optional, defaults to all)")
    p_check.add_argument("--fix", action="store_true", help="Auto-revert over-changed segments to conservative")
    p_check.add_argument("--diff", action="store_true", help="Show changed segments summary")
    p_check.add_argument("--diff-log", help="Write detailed before/after diff to FILE (spoiler warning included)")
    p_check.add_argument("--glossary", action="store_true", help="Verify glossary coverage: check HTML for un-replaced term keys")

    p_dump = sub.add_parser("dump-text", help="Dump all extracted text for LLM proofreading")
    p_dump.add_argument("work_dir", help="Work directory")
    p_dump.add_argument("--max-chars", type=int, default=100000,
                        help="Auto-split into batches if total exceeds N chars (default 100000)."
                             " Use 50000 for small-context models, 200000 for 1M+ models.")
    p_dump.add_argument("--clean-batches", action="store_true",
                        help="Strip [? ...] markers from batch text; write separate "
                             "*_checklist.json files.")

    p_apply = sub.add_parser("apply-corrections", help="Apply structured LLM corrections JSON")
    p_apply.add_argument("work_dir", help="Work directory")
    p_apply.add_argument("corrections_json", help="Path to Claude's JSON output file (or '-' for stdin)")

    p_extract_terms = sub.add_parser("extract-terms", help="Auto-extract new glossary terms from proofread output")
    p_extract_terms.add_argument("work_dir", help="Work directory")

    p_round3 = sub.add_parser("prepare-round3",
                              help="Prepare batches for Round 3 literary polishing "
                                   "(generates Round 3 mechanical markers in checklist files)")
    p_round3.add_argument("work_dir", help="Work directory")

    p_config = sub.add_parser("config", help="Show or reset config")
    p_config.add_argument("work_dir", help="Work directory")
    p_config.add_argument("--show", action="store_true", help="Show current config")
    p_config.add_argument("--reset", action="store_true", help="Reset config to default")

    args = parser.parse_args()

    if args.command == "init":
        return cmd_init(args)
    elif args.command == "extract":
        return cmd_extract(args)
    elif args.command == "preprocess":
        return cmd_preprocess(args)
    elif args.command == "reprocess":
        return cmd_reprocess(args)
    elif args.command == "inject":
        return cmd_inject(args)
    elif args.command == "pack":
        return cmd_pack(args)
    elif args.command == "pipeline":
        return cmd_pipeline(args)
    elif args.command == "add-term":
        if not add_term_to_glossary(args.term, args.translation, args.work_dir):
            print(f"  Term '{args.term}' unchanged (already '{args.translation}')")
        return 0
    elif args.command == "add-terms":
        try:
            terms_list = json.loads(args.terms_json, strict=False)
        except json.JSONDecodeError as e:
            print(f"  Error: invalid JSON: {e}")
            return 1
        added, updated, unchanged, _ = add_terms_batch(terms_list, args.work_dir)
        print(f"  Batch add: {added} added, {updated} updated, {unchanged} unchanged")
        return 0
    elif args.command == "check":
        return cmd_check(args)
    elif args.command == "dump-text":
        return cmd_dump_text(args)
    elif args.command == "apply-corrections":
        return cmd_apply_corrections(args)
    elif args.command == "extract-terms":
        return cmd_extract_terms(args)
    elif args.command == "prepare-round3":
        return cmd_prepare_round3(args)
    elif args.command == "config":
        cfg = load_config(args.work_dir)
        if args.reset:
            # Reset: keep only the defaults via DEFAULT_CONFIG_PATH
            if DEFAULT_CONFIG_PATH.exists():
                with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = {"blacklist": [], "proofreading": {}}
            save_config(cfg, args.work_dir)
            print(f"Config reset to default in {args.work_dir}")
        elif args.show:
            print(json.dumps(cfg, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
