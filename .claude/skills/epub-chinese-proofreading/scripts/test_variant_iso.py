"""
Isolated evaluation of each improvement independently.
Tests A, B, C separately and combined to identify optimal configuration.
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


def _score_and_rank(candidates, freq2, by_first, top_n, min_freq):
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


def _tokenize_and_freq(extracted_dir, max_token_len):
    _token_re = re.compile(r'[\u4e00-\u9fff]{2,' + str(max_token_len) + '}')
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
    return tokens, freq2


def find_variants_config(extracted_dir, top_n=50, min_freq=2,
                          max_token_len=4, relaxed_diff=False,
                          cross_substr=False):
    """Configurable variant detection for A/B/C testing."""
    tokens, freq2 = _tokenize_and_freq(extracted_dir, max_token_len)
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
                    if relaxed_diff and len(a) >= 4:
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

        if cross_substr:
            substr_groups = collections.defaultdict(list)
            for t in freq2:
                if len(t) >= 3:
                    for start in range(len(t) - 1):
                        key = (start, t[start:start+2])
                        g = substr_groups[key]
                        if len(g) < _MAX_SUFFIX_GROUP:
                            g.append(t)
            for (pos, substr), tkns in substr_groups.items():
                if len(tkns) < 2:
                    continue
                for i in range(len(tkns)):
                    a = tkns[i]
                    for j in range(i + 1, len(tkns)):
                        b = tkns[j]
                        if a[0] == b[0]:
                            continue
                        if len(a) != len(b):
                            continue
                        diff_chars = [a[k] for k in range(len(a)) if a[k] != b[k]]
                        diff_chars += [b[k] for k in range(len(b)) if a[k] != b[k]]
                        if any(c in _SEMANTIC_SUFFIXES and c not in _TRANSLITERATION_CHARS for c in diff_chars):
                            continue
                        candidates.add((a, b))

    return _score_and_rank(candidates, freq2, by_first, top_n, min_freq)


# ── Test corpus (same as test_variant_improvement.py) ─────────────────────────

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

VARIANTS_NEW = [
    ("查尔德里克", "奇尔德里克", 5, 3),
    ("艾格勒莫特", "埃格勒莫特", 4, 2),
    ("克里斯蒂安", "克里斯蒂恩", 3, 3),
    ("亚历山大德", "亚历山大特", 3, 2),
    ("巴拉克尔", "巴拉诺尔", 3, 2),
]

DISTRACTORS = [
    ("房间", 12), ("门口", 10), ("声音", 15), ("眼神", 8),
    ("地方", 6), ("东西", 5), ("回答", 10), ("看见", 7),
    ("似乎", 12), ("因为", 15), ("可以", 10), ("知道", 8),
    ("头发", 5), ("脚步", 4), ("呼吸", 5), ("心脏", 3),
    ("走廊", 3), ("蜡烛", 2), ("镜子", 2), ("窗户", 3),
    ("尼古拉", 3), ("卡特琳", 3), ("奥利弗", 3), ("罗兰德", 3),
    ("仆人", 3), ("酒杯", 3), ("花园", 3),
    ("尽管如此", 5), ("无论如何", 4), ("与此同时", 4), ("也就是说", 6),
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


def compute(found, ground):
    fset = {key(a, b) for a, b, _ in found}
    tp, fp, fn = fset & ground, fset - ground, ground - fset
    n_tp, n_fp, n_fn = len(tp), len(fp), len(fn)
    pr = n_tp / (n_tp + n_fp) if (n_tp + n_fp) > 0 else 0.0
    re = n_tp / (n_tp + n_fn) if (n_tp + n_fn) > 0 else 0.0
    f1 = 2 * pr * re / (pr + re) if (pr + re) > 0 else 0.0
    return n_tp, n_fp, n_fn, pr, re, f1


CONFIGS = [
    ("Baseline (current)",          dict(max_token_len=4, relaxed_diff=False, cross_substr=False)),
    ("A only (token 2→6)",          dict(max_token_len=6, relaxed_diff=False, cross_substr=False)),
    ("B only (relaxed diff)",       dict(max_token_len=4, relaxed_diff=True,  cross_substr=False)),
    ("A + B",                       dict(max_token_len=6, relaxed_diff=True,  cross_substr=False)),
    ("A + B + C (all)",             dict(max_token_len=6, relaxed_diff=True,  cross_substr=True)),
]


def run():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    try:
        build_corpus(tmpdir)
        ground = {key(a, b) for a, b, *_ in VARIANTS_ORIG + VARIANTS_NEW}

        print("=" * 80)
        print("ISOLATED IMPROVEMENT EVALUATION")
        print("=" * 80)
        print(f"{'Config':<30} {'TP':>5} {'FP':>5} {'FN':>5} {'Prec':>7} {'Recall':>7} {'F1':>7}")
        print("-" * 80)

        results = {}
        for name, cfg in CONFIGS:
            r = find_variants_config(tmpdir, top_n=100, min_freq=2, **cfg)
            tp, fp, fn, pr, re, f1 = compute(r, ground)
            results[name] = (tp, fp, fn, pr, re, f1)
            marker = " ← CURRENT" if "Baseline" in name else ""
            print(f"{name:<30} {tp:>5} {fp:>5} {fn:>5} {pr:>6.1%} {re:>7.1%} {f1:>7.3f}{marker}")

        # Compare best vs baseline
        best_name = max(results, key=lambda n: results[n][5])  # max F1
        bt, bt_fp, bt_fn, bp, br, bf1 = results["Baseline (current)"]
        tt, tt_fp, tt_fn, tp_, tr, tf1 = results[best_name]

        print(f"\nBest config: {best_name}")
        print(f"  Recall:  {br:.1%} → {tr:.1%}  (+{tr-br:+.1%})")
        print(f"  F1:      {bf1:.3f} → {tf1:.3f}  ({tf1-bf1:+.3f})")
        print(f"  FP cost: {bt_fp} → {tt_fp}  (+{tt_fp-bt_fp})")

        # Recommendation
        print(f"\n{'─'*80}")
        if tf1 > bf1 + 0.03:
            print("RECOMMENDATION: Apply the improvement to proofread.py")
        elif tr > br + 0.1:
            print("RECOMMENDATION: Apply (recall gain worth modest FP cost)")
        else:
            print("RECOMMENDATION: Keep current algorithm")
        print(f"{'─'*80}")
    finally:
        shutil.rmtree(tmpdir)


if __name__ == "__main__":
    run()
