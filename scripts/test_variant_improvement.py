"""
Compare current vs improved _find_suspected_variants on same corpus.

Improvements tested:
  A) Token length 2-4 → 2-6 (catch long transliterated names)
  B) Relaxed diff threshold for >=4 char same-length tokens (shared >= len-2)
  C) Cross-group: shared 2-char substring at ANY position (not just suffix)

Runs baseline + each improvement independently, reports delta.
"""
import json, math, re, sys, tempfile, pathlib, shutil, collections, os

os.environ["PYTHONIOENCODING"] = "utf-8"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_TRANSLITERATION_CHARS = set(
    "尔斯特克德拉利里格瑟林恩安伊奥亚维瓦塔诺卡莱蒙布罗纳达马尼加萨巴波索雷弗兰贝哈吉库穆菲珀瑞泰沃温扎赫洛莫佩普鲁塞希修雅约朱丹凯迪艾"
    "琳娜娅妮莉丝蕾黛珊桑瑰琪琦瑶翠芙芬芳蒂蓓薇"
    "昆坦顿敦伦伯格曼森登堡茨兹"
    "阿拜彼茨迦科柯勒梅奈涅帕皮齐日舍施韦沙"
    "耶撒门以兰夫吉麦丹耳列威尼黎但士来百内冰"
)
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
)
_MAX_GROUP_SIZE = 60
_MAX_SUFFIX_GROUP = 40


# ── Baseline: exact copy of current proofread.py logic ────────────────────────

def find_variants_baseline(extracted_dir, top_n=50, min_freq=2):
    _token_re = re.compile(r'[\u4e00-\u9fff]{2,4}')
    tokens = collections.Counter()
    for fpath in sorted(extracted_dir.glob("chapter_*_preprocessed.json")):
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for seg in data.get("segments", []):
            text = seg.get("content", "")
            for m in _token_re.finditer(text):
                raw = m.group()
                if raw[0] in _COMMON_STARTS:
                    continue
                if len(raw) >= 3 and raw[-1] not in _TRANSLITERATION_CHARS:
                    raw = raw[:-1]
                if len(raw) >= 2:
                    tokens[raw] += 1

    freq2 = {t: c for t, c in tokens.items() if c >= 2}
    prefix_idx = collections.defaultdict(set)
    for t in freq2:
        if len(t) >= 2:
            prefix_idx[t[:2]].add(t)
    for t, c in tokens.items():
        if t in freq2 or len(t) < 2:
            continue
        if t[:2] in prefix_idx:
            freq2[t] = 2

    if len(freq2) < 2:
        return []

    by_first = collections.defaultdict(list)
    for t in sorted(freq2, key=lambda x: -len(x)):
        g = by_first[t[0]]
        if len(g) < _MAX_GROUP_SIZE:
            g.append(t)

    candidates = set()
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
                        diff_chars = [a[k] for k in range(len(a)) if a[k] != b[k]]
                        diff_chars += [b[k] for k in range(len(b)) if a[k] != b[k]]
                        if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in diff_chars):
                            continue
                        candidates.add((a, b))
                elif len(a) >= 2 and len(b) >= 2:
                    if a[:2] == b[:2]:
                        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
                        extra = longer[len(shorter):]
                        if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in extra):
                            continue
                        candidates.add((a, b))

    # Cross-group: identical suffix chars[1:]
    if len(freq2) >= 2:
        suffix_groups = collections.defaultdict(list)
        for t in freq2:
            if len(t) >= 3:
                g = suffix_groups[t[1:]]
                if len(g) < _MAX_SUFFIX_GROUP:
                    g.append(t)
        for suffix, tkns in suffix_groups.items():
            if len(tkns) < 2:
                continue
            for i in range(len(tkns)):
                a = tkns[i]
                for j in range(i + 1, len(tkns)):
                    b = tkns[j]
                    if a[0] == b[0]:
                        continue
                    diff_chars = [a[0], b[0]]
                    if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in diff_chars):
                        continue
                    candidates.add((a, b))

    return _score_and_rank(candidates, freq2, by_first, top_n, min_freq)


# ── Improved: token length 2-6 (A) + relaxed diff (B) + cross-group substring (C) ─

def find_variants_improved(extracted_dir, top_n=50, min_freq=2):
    _token_re = re.compile(r'[\u4e00-\u9fff]{2,6}')  # (A) 2-4 → 2-6
    tokens = collections.Counter()
    for fpath in sorted(extracted_dir.glob("chapter_*_preprocessed.json")):
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for seg in data.get("segments", []):
            text = seg.get("content", "")
            for m in _token_re.finditer(text):
                raw = m.group()
                if raw[0] in _COMMON_STARTS:
                    continue
                if len(raw) >= 3 and raw[-1] not in _TRANSLITERATION_CHARS:
                    raw = raw[:-1]
                if len(raw) >= 2:
                    tokens[raw] += 1

    freq2 = {t: c for t, c in tokens.items() if c >= 2}
    prefix_idx = collections.defaultdict(set)
    for t in freq2:
        if len(t) >= 2:
            prefix_idx[t[:2]].add(t)
    for t, c in tokens.items():
        if t in freq2 or len(t) < 2:
            continue
        if t[:2] in prefix_idx:
            freq2[t] = 2

    if len(freq2) < 2:
        return []

    by_first = collections.defaultdict(list)
    for t in sorted(freq2, key=lambda x: -len(x)):
        g = by_first[t[0]]
        if len(g) < _MAX_GROUP_SIZE:
            g.append(t)

    candidates = set()
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
                    # (B) Relax diff threshold: >=4 char tokens allow 2 diffs
                    if len(a) >= 4:
                        min_shared = len(a) - 2
                    else:
                        min_shared = len(a) - 1
                    if shared >= max(1, min_shared):
                        diff_chars = [a[k] for k in range(len(a)) if a[k] != b[k]]
                        diff_chars += [b[k] for k in range(len(b)) if a[k] != b[k]]
                        if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in diff_chars):
                            continue
                        candidates.add((a, b))
                elif len(a) >= 2 and len(b) >= 2:
                    if a[:2] == b[:2]:
                        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
                        extra = longer[len(shorter):]
                        if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in extra):
                            continue
                        candidates.add((a, b))

    # Cross-group: identical suffix chars[1:] (original) + 2-char substring anywhere (C)
    if len(freq2) >= 2:
        # Original: same suffix (chars[1:])
        suffix_groups = collections.defaultdict(list)
        for t in freq2:
            if len(t) >= 3:
                g = suffix_groups[t[1:]]
                if len(g) < _MAX_SUFFIX_GROUP:
                    g.append(t)
        for suffix, tkns in suffix_groups.items():
            if len(tkns) < 2:
                continue
            for i in range(len(tkns)):
                a = tkns[i]
                for j in range(i + 1, len(tkns)):
                    b = tkns[j]
                    if a[0] == b[0]:
                        continue
                    diff_chars = [a[0], b[0]]
                    if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in diff_chars):
                        continue
                    candidates.add((a, b))

        # (C) New: shared 2-char substring at any position for same-length tokens
        # with different first chars. Catches pairs like 爱卢亚/艾露亚 where
        # the middle char differs but first AND last are not the same.
        # Build index: for 3-char tokens, group by last-2-chars OR first-2-chars
        # For 4+ char tokens, group by any consecutive 2-char window.
        substr_groups = collections.defaultdict(list)
        for t in freq2:
            if len(t) >= 3:
                for start in range(len(t) - 1):
                    key = (start, t[start:start+2])
                    g = substr_groups[key]
                    if len(g) < _MAX_SUFFIX_GROUP:
                        g.append(t)
        seen_cg = set()
        for (pos, substr), tkns in substr_groups.items():
            if len(tkns) < 2:
                continue
            for i in range(len(tkns)):
                a = tkns[i]
                for j in range(i + 1, len(tkns)):
                    b = tkns[j]
                    if a[0] == b[0]:
                        continue  # already covered by within-group
                    if len(a) != len(b):
                        continue  # only same-length for cross-group
                    # Verify they actually differ in first char (cross-group condition)
                    pair_key = tuple(sorted([a, b]))
                    if pair_key in seen_cg:
                        continue
                    seen_cg.add(pair_key)
                    diff_chars = [a[k] for k in range(len(a)) if a[k] != b[k]]
                    diff_chars += [b[k] for k in range(len(b)) if a[k] != b[k]]
                    if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in diff_chars):
                        continue
                    candidates.add((a, b))

    return _score_and_rank(candidates, freq2, by_first, top_n, min_freq)


def _score_and_rank(candidates, freq2, by_first, top_n, min_freq):
    """Shared scoring logic for both baseline and improved."""
    seen = set()
    result = []
    for a, b in candidates:
        key = tuple(sorted([a, b]))
        if key not in seen:
            seen.add(key)
            if freq2.get(a, 0) < min_freq or freq2.get(b, 0) < min_freq:
                continue
            shared = sum(1 for k in range(min(len(a), len(b))) if a[k] == b[k])
            max_len = max(len(a), len(b))
            freq = freq2.get(a, 0) + freq2.get(b, 0)
            score = (shared / max_len) * math.log(freq + 1)
            group_key = a[0]
            result.append((a, b, freq, score, group_key))
    result.sort(key=lambda x: (-x[3], -x[2]))

    top = list(result)
    small_groups = {ch for ch, grp in by_first.items() if 2 <= len(grp) <= 10}
    groups_in_top = {item[4] for item in top[:top_n]}
    missing = small_groups - groups_in_top
    if missing:
        group_best = {}
        for item in result:
            gk = item[4]
            if gk in missing and gk not in group_best:
                group_best[gk] = item
        n_replace = min(len(missing), top_n // 4)
        if n_replace > 0:
            extras = sorted(group_best.values(), key=lambda x: -x[3])[:n_replace]
            top = top[:top_n - n_replace] + extras
            top.sort(key=lambda x: -x[3])

    return [(a, b, f) for a, b, f, *_ in top[:top_n]]


# ── Test corpus ───────────────────────────────────────────────────────────────

# Original variants from test_variant_detection.py
VARIANTS_ORIG = [
    ("海辛瑟", "海辛德", 5, 3), ("海辛穆", "海辛特", 4, 2),
    ("德拉奈", "德拉纳", 4, 3), ("德拉穆", "德拉特", 3, 2),
    ("库什艾", "库什尔", 5, 3), ("库什穆", "库什特", 3, 2),
    ("巴拉", "巴拉克尔", 4, 2), ("维拉", "维拉诺斯", 3, 3),
    ("艾格", "艾格勒莫", 3, 1), ("克鲁伊特", "克鲁伊德", 4, 3),
    ("克鲁穆尔", "克鲁特斯", 2, 2),
    ("卡西", "卡西林顿", 4, 2),
    ("约舒亚", "约舒德", 3, 3), ("米歇", "米歇尔顿", 3, 1),
    ("约书亚", "耶苏亚", 4, 2), ("爱卢亚", "艾露亚", 3, 3),
    ("米歇尔", "密歇尔", 3, 2), ("卡西林", "卡西林特", 4, 2),
    ("维拉斯", "维拉斯特", 4, 2),
]

# New variants that test improvements A and B
VARIANTS_NEW = [
    # (A) 5-6 char names — only caught when token length extends to 6
    ("查尔德里克", "奇尔德里克", 5, 3),   # 5-char, same first chars 查/奇 differ
    ("艾格勒莫特", "埃格勒莫特", 4, 2),   # 5-char, same-length, diff in pos 0
    ("克里斯蒂安", "克里斯蒂恩", 3, 3),   # 5-char, differ in last char
    ("亚历山大德", "亚历山大特", 3, 2),   # 5-char, differ in last 2 chars (B)

    # (B) 4-char tokens differing in 2 positions — only caught with relaxed threshold
    ("巴拉克尔", "巴拉诺尔", 3, 2),        # 4-char, share "巴拉", diff "克尔" vs "诺尔"
    ("克鲁穆尔", "克鲁特斯", 2, 2),        # already in orig, tests (B) for recall
]

# Distractors
DISTRACTORS = [
    ("房间", 12), ("门口", 10), ("声音", 15), ("眼神", 8),
    ("地方", 6), ("东西", 5), ("回答", 10), ("看见", 7),
    ("似乎", 12), ("因为", 15), ("可以", 10), ("知道", 8),
    ("头发", 5), ("脚步", 4), ("呼吸", 5), ("心脏", 3),
    ("走廊", 3), ("蜡烛", 2), ("镜子", 2), ("窗户", 3),
    ("尼古拉", 3), ("卡特琳", 3), ("奥利弗", 3), ("罗兰德", 3),
    ("仆人", 3), ("酒杯", 3), ("花园", 3),
    # Longer distractors (5-6 char common phrases, not names)
    ("尽管如此", 5), ("无论如何", 4), ("与此同时", 4),
    ("也就是说", 3), ("也就是说", 0),  # duplicate to reach effective 6
]


def build_corpus(tmpdir):
    items = []
    for a, b, ca, cb in VARIANTS_ORIG + VARIANTS_NEW:
        items.extend([a] * ca)
        items.extend([b] * cb)
    for word, count in DISTRACTORS:
        items.extend([word] * count)

    import random
    random.Random(12345).shuffle(items)

    segments = []
    for i in range(0, len(items), 5):
        batch = items[i:i+5]
        content = "\n".join(batch)
        segments.append({"content": content, "id": f"seg_{len(segments)}"})

    with open(tmpdir / "chapter_0001_preprocessed.json", "w", encoding="utf-8") as f:
        json.dump({"segments": segments}, f, ensure_ascii=False)


def key(a, b):
    return tuple(sorted([a, b]))


def compute_metrics(found, ground):
    fset = {key(a, b) for a, b, _ in found}
    tp = fset & ground
    fp = fset - ground
    fn = ground - fset
    n_tp, n_fp, n_fn = len(tp), len(fp), len(fn)
    pr = n_tp / (n_tp + n_fp) if (n_tp + n_fp) > 0 else 0.0
    re = n_tp / (n_tp + n_fn) if (n_tp + n_fn) > 0 else 0.0
    f1 = 2 * pr * re / (pr + re) if (pr + re) > 0 else 0.0
    return tp, fp, fn, pr, re, f1


def run_test():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    try:
        build_corpus(tmpdir)

        # Run both algorithms against identical corpus
        r_base = find_variants_baseline(tmpdir, top_n=100, min_freq=2)
        r_impr = find_variants_improved(tmpdir, top_n=100, min_freq=2)

        all_variants = VARIANTS_ORIG + VARIANTS_NEW
        ground_all = {key(a, b) for a, b, *_ in all_variants}
        ground_orig = {key(a, b) for a, b, *_ in VARIANTS_ORIG}
        ground_new = {key(a, b) for a, b, *_ in VARIANTS_NEW}

        # ── Report ──
        print("=" * 74)
        print("VARIANT DETECTION IMPROVEMENT EVALUATION")
        print("=" * 74)
        print(f"Ground truth: {len(ground_all)} pairs ({len(ground_orig)} orig + {len(ground_new)} new)")

        tp_b, fp_b, fn_b, pr_b, re_b, f1_b = compute_metrics(r_base, ground_all)
        tp_i, fp_i, fn_i, pr_i, re_i, f1_i = compute_metrics(r_impr, ground_all)

        print(f"\n{'':>35} {'Baseline':>12} {'Improved':>12} {'Delta':>10}")
        print("-" * 70)
        print(f"{'True Positives':>35} {len(tp_b):>12} {len(tp_i):>12} {len(tp_i)-len(tp_b):>+10}")
        print(f"{'False Positives':>35} {len(fp_b):>12} {len(fp_i):>12} {len(fp_i)-len(fp_b):>+10}")
        print(f"{'False Negatives':>35} {len(fn_b):>12} {len(fn_i):>12} {len(fn_i)-len(fn_b):>+10}")
        print(f"{'Precision':>35} {pr_b:>11.1%} {pr_i:>11.1%} {pr_i-pr_b:>+10.1%}")
        print(f"{'Recall':>35} {re_b:>11.1%} {re_i:>11.1%} {re_i-re_b:>+10.1%}")
        print(f"{'F1 Score':>35} {f1_b:>11.3f} {f1_i:>11.3f} {f1_i-f1_b:>+10.3f}")

        # Per-pair detail
        print(f"\n{'Pair':<30} {'Freq':>7} {'Base':>6} {'Impr':>6} {'Note'}")
        print("-" * 65)
        for a, b, ca, cb in all_variants:
            k = key(a, b)
            in_base = "YES" if k in {key(x, y) for x, y, _ in r_base} else "no"
            in_impr = "YES" if k in {key(x, y) for x, y, _ in r_impr} else "no"
            note = ""
            if in_base == "no" and in_impr == "YES":
                note = " <<< NEWLY CAUGHT"
            elif in_base == "YES" and in_impr == "no":
                note = " ??? REGRESSION"
            elif in_base == "no" and in_impr == "no":
                if len(a) >= 5 or len(b) >= 5:
                    note = " (long token, caught only if >=5 CJK chars)"
                else:
                    note = " (structurally undetectable)"
            print(f"{a}/{b:<22} {ca}+{cb:>4} {in_base:>6} {in_impr:>6}{note}")

        # New false positives from improved algorithm
        new_fp = {key(x, y) for x, y, _ in r_impr} - {key(x, y) for x, y, _ in r_base} - ground_all
        if new_fp:
            print(f"\nNew False Positives from Improved ({len(new_fp)}):")
            for k in sorted(new_fp):
                print(f"  {k[0]} <-> {k[1]}")

        # Verdict
        print(f"\n{'='*74}")
        delta_f1 = f1_i - f1_b
        delta_recall = re_i - re_b
        if delta_f1 > 0.05:
            verdict = "CLEAR IMPROVEMENT — apply to proofread.py"
        elif delta_f1 > 0.01:
            verdict = "MODEST IMPROVEMENT — apply if FP increase acceptable"
        elif delta_recall > 0:
            verdict = "RECALL ONLY — check FP cost before applying"
        else:
            verdict = "NO IMPROVEMENT — keep current algorithm"

        print(f"F1 delta: {delta_f1:+.3f} | Recall delta: {delta_recall:+.1%}")
        print(f"Verdict: {verdict}")
        print("=" * 74)

        return f1_i > f1_b
    finally:
        shutil.rmtree(tmpdir)


if __name__ == "__main__":
    improved = run_test()
    sys.exit(0 if improved else 1)
