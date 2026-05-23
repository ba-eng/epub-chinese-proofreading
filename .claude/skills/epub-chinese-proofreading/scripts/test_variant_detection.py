"""
FINAL clean regression test for _find_suspected_variants() threshold change.

Design rules:
- Every token is UNIQUE across all pairs (no frequency contamination)
- Each first-char group has at most 2 variant pairs (realistic density)
- Same-length pairs: differ by exactly 1 char
- Diff-length pairs: share 2-char prefix, differ by >=2 chars
- Singleton rescue: one token appears exactly once

Measures recall/precision/F1 at thresh=3 vs thresh=2 against SAME ground truth.
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
_TOKEN_RE = re.compile(r'[\u4e00-\u9fff]{2,4}')
_MAX_GROUP_SIZE = 60


def find_variants(extracted_dir, top_n=50, min_freq=3):
    tokens = collections.Counter()
    for fpath in sorted(extracted_dir.glob("chapter_*_preprocessed.json")):
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for seg in data.get("segments", []):
            text = seg.get("content", "")
            for m in _TOKEN_RE.finditer(text):
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

    candidates = []
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
                        candidates.append((a, b))
                elif len(a) >= 2 and len(b) >= 2:
                    if a[:2] == b[:2]:
                        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
                        extra = longer[len(shorter):]
                        if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in extra):
                            continue
                        candidates.append((a, b))

    # Cross-group comparison (FIX 2): same-length tokens with matching
    # suffix but different first chars (e.g., 约书亚/耶苏亚)
    _MAX_SUFFIX_GROUP = 40
    if len(freq2) >= 2:
        suffix_groups = collections.defaultdict(list)
        for t in freq2:
            if len(t) >= 3:
                g = suffix_groups[t[1:]]
                if len(g) < _MAX_SUFFIX_GROUP:
                    g.append(t)
        for suffix, tokens in suffix_groups.items():
            if len(tokens) < 2:
                continue
            for i in range(len(tokens)):
                a = tokens[i]
                for j in range(i + 1, len(tokens)):
                    b = tokens[j]
                    if a[0] == b[0]:
                        continue
                    diff_chars = [a[0], b[0]]
                    if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in diff_chars):
                        continue
                    candidates.append((a, b))

    seen = set()
    result = []
    for a, b in candidates:
        key = tuple(sorted([a, b]))
        if key not in seen:
            seen.add(key)
            # Proper-prefix filter REMOVED (FIX 1) — was blocking
            # genuine name-suffix variants. Semantic suffix filter
            # in candidate-formation already handles common suffixes.
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


# ── TRULY UNIQUE test pairs ─────────────────────────────────────────────────
# Each token appears in EXACTLY ONE pair. No cross-contamination.
# Spread across different first-char groups (海, 德, 库, 巴, 维, 艾, 克, 卡, 约, 米)
# Each group: 1-2 pairs max (realistic density).

# Format: (token_a, token_b, count_a, count_b)
# All same-length pairs: 3-char tokens, differ exactly in position 2 (last char)
# All diff-length pairs: 2-char base vs 4-char extended (diff=2 chars)

VARIANTS = [
    # ── Group "海": 2 pairs (same-length) ──
    ("海辛瑟", "海辛德", 5, 3),    # both >=3 -> caught by both thresholds
    ("海辛穆", "海辛特", 4, 2),    # 4+2 -> caught only by thresh=2

    # ── Group "德": 2 pairs (same-length) ──
    ("德拉奈", "德拉纳", 4, 3),    # both >=3 -> caught by both
    ("德拉穆", "德拉特", 3, 2),    # 3+2 -> caught only by thresh=2

    # ── Group "库": 2 pairs (same-length) ──
    ("库什艾", "库什尔", 5, 3),    # both >=3 -> caught by both
    ("库什穆", "库什特", 3, 2),    # 3+2 -> caught only by thresh=2

    # ── Group "巴": 1 diff-length pair ──
    ("巴拉", "巴拉克尔", 4, 2),    # 2 vs 4 chars, diff=2 -> caught only by thresh=2

    # ── Group "维": 1 diff-length pair ──
    ("维拉", "维拉诺斯", 3, 3),    # 2 vs 4 chars, diff=2 -> caught by both

    # ── Group "艾": 1 singleton rescue ──
    ("艾格", "艾格勒莫", 3, 1),    # 2 vs 4 chars, singleton rescue -> caught only by thresh=2

    # ── Group "克": 2 same-length pairs ──
    ("克鲁伊特", "克鲁伊德", 4, 3),# both >=3 -> caught by both
    ("克鲁穆尔", "克鲁特斯", 2, 2),# 2+2 -> caught only by thresh=2

    # ── Group "卡": 1 diff-length pair ──
    ("卡西", "卡西林顿", 4, 2),    # 2 vs 4 chars, diff=2 -> caught only by thresh=2

    # ── Group "约": 1 same-length pair ──
    ("约舒亚", "约舒德", 3, 3),    # both >=3 -> caught by both

    # ── Group "米": 1 singleton rescue ──
    ("米歇", "米歇尔顿", 3, 1),    # 2 vs 4 chars, singleton -> caught only by thresh=2

    # ── Cross-group pairs (FIX 2): different first chars, same suffix ──
    ("约书亚", "耶苏亚", 4, 2),    # both 3-char, share "书亚", diff first char -> FIX 2
    ("爱卢亚", "艾露亚", 3, 3),    # both 3-char, share "卢亚"? No — "卢亚" vs "露亚" differ!
    # Wait: 爱卢亚 = 爱/卢/亚, 艾露亚 = 艾/露/亚 → suffix[1:] = "卢亚" vs "露亚" → DIFFERENT!
    # They don't share the same suffix, so cross-group won't catch them.
    # These are truly different transliterations of the same name — need LLM to catch.
    ("米歇尔", "密歇尔", 3, 2),    # both 3-char, share "歇尔", diff first char -> FIX 2

    # ── Proper-prefix pairs (FIX 1): diff=1 now allowed ──
    ("卡西林", "卡西林特", 4, 2),  # 3 vs 4 chars, diff=1 prefix-suffix -> FIX 1
    ("维拉斯", "维拉斯特", 4, 2),  # 3 vs 4 chars, diff=1 -> FIX 1
]

GROUND_TRUTH = {tuple(sorted([a, b])) for a, b, *_ in VARIANTS}


def effective_freq(c):
    if c >= 2: return c
    if c == 1: return 2
    return 0


EXPECTED_3 = set()
EXPECTED_2 = set()
for a, b, ca, cb in VARIANTS:
    k = tuple(sorted([a, b]))
    if effective_freq(ca) >= 3 and effective_freq(cb) >= 3:
        EXPECTED_3.add(k)
    if effective_freq(ca) >= 2 and effective_freq(cb) >= 2:
        EXPECTED_2.add(k)


# ── Distractors (different first chars from all variant groups) ─────────────
DISTRACTORS = [
    ("房间", 12), ("门口", 10), ("声音", 15), ("眼神", 8),
    ("地方", 6), ("东西", 5), ("回答", 10), ("看见", 7),
    ("似乎", 12), ("因为", 15), ("可以", 10), ("知道", 8),
    ("头发", 5), ("脚步", 4), ("呼吸", 5), ("心脏", 3),
    ("走廊", 3), ("蜡烛", 2), ("镜子", 2), ("窗户", 3),
    ("尼古拉", 3), ("卡特琳", 3), ("奥利弗", 3), ("罗兰德", 3),
    ("仆人", 3), ("酒杯", 3), ("花园", 3),
]


def build_corpus(tmpdir):
    items = []
    for a, b, ca, cb in VARIANTS:
        items.extend([a] * ca)
        items.extend([b] * cb)
    for word, count in DISTRACTORS:
        items.extend([word] * count)

    import random
    random.Random(12345).shuffle(items)

    segments = []
    for i in range(0, len(items), 4):
        batch = items[i:i+4]
        content = "\n".join(batch)
        segments.append({"content": content, "id": f"seg_{len(segments)}"})

    with open(tmpdir / "chapter_0001_preprocessed.json", "w", encoding="utf-8") as f:
        json.dump({"segments": segments}, f, ensure_ascii=False)


def key(a, b):
    return tuple(sorted([a, b]))


def run_test():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    try:
        build_corpus(tmpdir)
        r3 = find_variants(tmpdir, top_n=100, min_freq=3)
        r2 = find_variants(tmpdir, top_n=100, min_freq=2)

        p3 = {key(a, b) for a, b, _ in r3}
        p2 = {key(a, b) for a, b, _ in r2}

        tp3, fp3, fn3 = p3 & GROUND_TRUTH, p3 - GROUND_TRUTH, GROUND_TRUTH - p3
        tp2, fp2, fn2 = p2 & GROUND_TRUTH, p2 - GROUND_TRUTH, GROUND_TRUTH - p2

        def m(tp, fp, fn):
            n_tp, n_fp, n_fn = len(tp), len(fp), len(fn)
            pr = n_tp / (n_tp + n_fp) if (n_tp + n_fp) > 0 else 0.0
            re = n_tp / (n_tp + n_fn) if (n_tp + n_fn) > 0 else 0.0
            f1 = 2 * pr * re / (pr + re) if (pr + re) > 0 else 0.0
            return pr, re, f1

        p3v, r3v, f13 = m(tp3, fp3, fn3)
        p2v, r2v, f12 = m(tp2, fp2, fn2)

        print("=" * 72)
        print("FINAL REGRESSION: _find_suspected_variants() threshold")
        print("=" * 72)
        print(f"Ground truth pairs: {len(GROUND_TRUTH)}")
        print(f"  Expected caught @3: {len(EXPECTED_3)}, @2: {len(EXPECTED_2)}")
        print(f"Distractors: {len(DISTRACTORS)}")
        print(f"Unique variant tokens: {len(VARIANTS)*2} (no cross-pair sharing)")

        print(f"\n{'':>30} {'Thresh>=3':>15} {'Thresh>=2':>15} {'Delta':>10}")
        print("-" * 71)
        print(f"{'True Positives':>30} {len(tp3):>15} {len(tp2):>15} {len(tp2)-len(tp3):>+10}")
        print(f"{'False Positives':>30} {len(fp3):>15} {len(fp2):>15} {len(fp2)-len(fp3):>+10}")
        print(f"{'False Negatives':>30} {len(fn3):>15} {len(fn2):>15} {len(fn2)-len(fn3):>+10}")
        print(f"{'Precision':>30} {p3v:>14.1%} {p2v:>14.1%} {p2v-p3v:>+10.1%}")
        print(f"{'Recall':>30} {r3v:>14.1%} {r2v:>14.1%} {r2v-r3v:>+10.1%}")
        print(f"{'F1 Score':>30} {f13:>14.3f} {f12:>14.3f} {f12-f13:>+10.3f}")

        # Per-pair detail
        print(f"\n{'Pair':<26} {'Freq':>7} {'@3':>5} {'@2':>5} {'Expected'}")
        print("-" * 55)
        all_ok = True
        for a, b, ca, cb in VARIANTS:
            k = key(a, b)
            g3 = "YES" if k in p3 else "no"
            g2 = "YES" if k in p2 else "no"
            exp3 = k in EXPECTED_3
            exp2 = k in EXPECTED_2
            ok = (k in p2) == exp2
            if not ok:
                all_ok = False
            exp_str = f"@3={'Y' if exp3 else 'N'} @2={'Y' if exp2 else 'N'}"
            tag = " <<<" if not ok else ""
            print(f"{a}/{b:<19} {ca}+{cb:>4} {g3:>5} {g2:>5} {exp_str}{tag}")

        if fp3:
            print(f"\nFalse Positives @3 ({len(fp3)}):")
            for k in sorted(fp3):
                print(f"  {k[0]} <-> {k[1]}")
        if fp2:
            print(f"\nFalse Positives @2 ({len(fp2)}):")
            for k in sorted(fp2):
                print(f"  {k[0]} <-> {k[1]}")

        if fn2:
            print(f"\nFalse Negatives @2 ({len(fn2)}):")
            for k in sorted(fn2):
                print(f"  {k[0]} <-> {k[1]}")

        print(f"\n{'='*72}")
        print(f"Recall gain:  {r3v:.0%} -> {r2v:.0%}  (+{r2v-r3v:+.0%})")
        print(f"Precision:    {p3v:.0%} -> {p2v:.0%}  ({p2v-p3v:+.0%})")
        print(f"F1:           {f13:.3f} -> {f12:.3f}  ({f12-f13:+.3f})")

        if r2v - r3v > 0.20:
            print("VERDICT: SUBSTANTIAL recall improvement from threshold=2")
        elif r2v - r3v > 0.05:
            print("VERDICT: Meaningful recall improvement from threshold=2")
        else:
            print("VERDICT: Minimal impact (structural filters bottleneck)")
        print("=" * 72)
        return all_ok
    finally:
        shutil.rmtree(tmpdir)


if __name__ == "__main__":
    ok = run_test()
    sys.exit(0 if ok else 1)
