"""
Regression test for production _find_suspected_variants().

Design rules:
- The test calls the live implementation in proofread.py, not a copied heuristic.
- Every expected token pair is isolated enough to avoid cross-pair contamination.
- Same-length pairs differ by exactly 1 char.
- Diff-length pairs share a 2-char prefix.
- Singleton rescue: one token appears exactly once but shares a 2-char prefix.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

os.environ["PYTHONIOENCODING"] = "utf-8"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proofread import _find_suspected_variants


# Format: (token_a, token_b, count_a, count_b)
VARIANTS = [
    # Same-length pairs
    ("海辛瑟", "海辛德", 5, 3),
    ("海拉穆", "海拉特", 4, 2),
    ("德拉奈", "德拉纳", 4, 3),
    ("德莫穆", "德莫特", 3, 2),
    ("库什艾", "库什尔", 5, 3),
    ("库曼穆", "库曼特", 3, 2),
    ("克鲁伊特", "克鲁伊德", 4, 3),
    ("克萨穆尔", "克萨特尔", 2, 2),
    ("威舒亚", "威舒德", 3, 3),

    # Diff-length pairs
    ("巴拉", "巴拉克尔", 4, 2),
    ("维拉", "维拉诺斯", 3, 3),
    ("艾格", "艾格勒莫", 3, 1),
    ("卡西", "卡西林顿", 4, 2),
    ("瑞岚", "瑞岚特斯", 3, 1),

    # Cross-group suffix pairs
    ("维书亚", "耶书亚", 4, 2),
    ("米歇尔", "米歇尔夫", 3, 2),

    # Proper-prefix pairs
    ("卡莫林", "卡莫林特", 4, 2),
    ("维诺斯", "维诺斯特", 4, 2),
]

# Known limitation: same referent, but the current heuristic does not catch it
# because neither the first 2 chars nor suffix[1:] match.
KNOWN_UNDETECTABLE = [
    ("爱卢亚", "艾露亚", 3, 3),
]

GROUND_TRUTH = {tuple(sorted([a, b])) for a, b, *_ in VARIANTS + KNOWN_UNDETECTABLE}
EXPECTED_PRODUCTION = {tuple(sorted([a, b])) for a, b, *_ in VARIANTS}


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
    for a, b, ca, cb in VARIANTS + KNOWN_UNDETECTABLE:
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


def metrics(tp, fp, fn):
    n_tp, n_fp, n_fn = len(tp), len(fp), len(fn)
    precision = n_tp / (n_tp + n_fp) if (n_tp + n_fp) else 0.0
    recall = n_tp / (n_tp + n_fn) if (n_tp + n_fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def run_test():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    try:
        build_corpus(tmpdir)
        results = _find_suspected_variants(tmpdir, top_n=100)
        detected = {key(a, b) for a, b, _ in results}

        tp = detected & GROUND_TRUTH
        fp = detected - GROUND_TRUTH
        fn = GROUND_TRUTH - detected
        precision, recall, f1 = metrics(tp, fp, fn)

        missed_expected = EXPECTED_PRODUCTION - detected
        unexpected = detected - EXPECTED_PRODUCTION
        all_ok = not missed_expected and not unexpected

        print("=" * 72)
        print("PRODUCTION REGRESSION: _find_suspected_variants()")
        print("=" * 72)
        print(f"Ground truth pairs: {len(GROUND_TRUTH)}")
        print(f"Expected production detections: {len(EXPECTED_PRODUCTION)}")
        print(f"Actual production detections: {len(detected)}")
        print(f"Distractors: {len(DISTRACTORS)}")
        print(f"Unique variant tokens: {(len(VARIANTS) + len(KNOWN_UNDETECTABLE)) * 2}")

        print(f"\n{'Metric':>20} {'Value':>12}")
        print("-" * 34)
        print(f"{'True Positives':>20} {len(tp):>12}")
        print(f"{'False Positives':>20} {len(fp):>12}")
        print(f"{'False Negatives':>20} {len(fn):>12}")
        print(f"{'Precision':>20} {precision:>11.1%}")
        print(f"{'Recall':>20} {recall:>11.1%}")
        print(f"{'F1 Score':>20} {f1:>12.3f}")

        print(f"\n{'Pair':<26} {'Freq':>7} {'Detected':>10} {'Expected'}")
        print("-" * 62)
        for a, b, ca, cb in VARIANTS + KNOWN_UNDETECTABLE:
            k = key(a, b)
            got = "YES" if k in detected else "no"
            exp = "YES" if k in EXPECTED_PRODUCTION else "no"
            ok = (k in detected) == (k in EXPECTED_PRODUCTION)
            tag = " <<<" if not ok else ""
            print(f"{a}/{b:<19} {ca}+{cb:>4} {got:>10} {exp:>8}{tag}")

        if missed_expected:
            print(f"\nMissed expected detections ({len(missed_expected)}):")
            for k in sorted(missed_expected):
                print(f"  {k[0]} <-> {k[1]}")

        if unexpected:
            print(f"\nUnexpected detections ({len(unexpected)}):")
            for k in sorted(unexpected):
                print(f"  {k[0]} <-> {k[1]}")

        print("=" * 72)
        print("VERDICT: PASS" if all_ok else "VERDICT: FAIL")
        print("=" * 72)
        return all_ok
    finally:
        shutil.rmtree(tmpdir)


if __name__ == "__main__":
    ok = run_test()
    sys.exit(0 if ok else 1)
