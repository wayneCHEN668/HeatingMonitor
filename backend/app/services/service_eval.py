"""客服诉求分类评测：python -m app.services.service_eval [标注文件 ...]

默认评测 knowledge/eval_service_requests.json（开发集：设计规则时看过）与
knowledge/eval_service_requests_heldout.json（留出集：分类器提交后才写，只评测一次）。
输出：类别准确率、各类别精确率/召回率、混淆矩阵、紧急与转人工两个二分类的精确率/召回率，以及全部错例。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from .service_desk import CATEGORIES, classify_request

KDIR = Path(__file__).resolve().parents[2] / "knowledge"
DEFAULT = [KDIR / "eval_service_requests.json", KDIR / "eval_service_requests_heldout.json"]


def _pr(tp: int, fp: int, fn: int) -> tuple[float | None, float | None]:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    return p, r


def evaluate(items: list[dict]) -> dict:
    conf = Counter()
    errors = []
    bin_counts = {k: Counter() for k in ("urgent", "human")}
    bin_counts["urgent_routed"] = Counter()
    for it in items:
        out = classify_request(it["text"])
        conf[(it["category"], out["category"])] += 1
        for k in ("urgent", "human"):
            g, p = bool(it[k]), bool(out[k])
            bin_counts[k]["tp" if g and p else "fp" if p else "fn" if g else "tn"] += 1
        if it["urgent"]:
            bin_counts["urgent_routed"]["caught" if out["urgent"] or out["human"] or out["confidence"] == "低" else "missed"] += 1
        wrong = [k for k in ("category", "urgent", "human") if it[k] != out[k]]
        if wrong:
            errors.append({"text": it["text"], "wrong": wrong, "gold": {k: it[k] for k in ("category", "urgent", "human")},
                           "pred": {k: out[k] for k in ("category", "urgent", "human")}})
    n = len(items)
    per_cat = {}
    for c in CATEGORIES:
        tp = conf[(c, c)]
        fp = sum(v for (g, p), v in conf.items() if p == c and g != c)
        fn = sum(v for (g, p), v in conf.items() if g == c and p != c)
        p, r = _pr(tp, fp, fn)
        per_cat[c] = {"support": tp + fn, "precision": p, "recall": r}
    binary = {}
    routed = bin_counts.pop("urgent_routed")
    for k, b in bin_counts.items():
        p, r = _pr(b["tp"], b["fp"], b["fn"])
        binary[k] = {"positives": b["tp"] + b["fn"], "precision": p, "recall": r, "accuracy": (b["tp"] + b["tn"]) / n,
                     **{x: b[x] for x in ("tp", "fp", "fn", "tn")}}
    return {"n": n, "category_accuracy": sum(conf[(c, c)] for c in CATEGORIES) / n,
            "all_correct": (n - len(errors)) / n, "per_category": per_cat, "binary": binary,
            "urgent_routed": {"caught": routed["caught"], "missed": routed["missed"]},
            "confusion": {g: {p: conf[(g, p)] for p in CATEGORIES} for g in CATEGORIES}, "errors": errors}


def _f(v):
    return "  —  " if v is None else f"{v:.2f}"


def report(path: Path) -> dict:
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    r = evaluate(items)
    print(f"\n=== {path.name}（{r['n']} 条）===")
    print(f"类别准确率 {r['category_accuracy']:.1%}；三项（类别+紧急+转人工）全对 {r['all_correct']:.1%}")
    print("类别        支持  精确率  召回率")
    for c, m in r["per_category"].items():
        print(f"{c:<8}{m['support']:>5}   {_f(m['precision'])}   {_f(m['recall'])}")
    for k, name in (("urgent", "紧急"), ("human", "转人工")):
        b = r["binary"][k]
        print(f"{name}：正例 {b['positives']}，精确率 {_f(b['precision'])}，召回率 {_f(b['recall'])}，"
              f"TP {b['tp']} FP {b['fp']} FN {b['fn']} TN {b['tn']}")
    u = r["urgent_routed"]
    if u["caught"] + u["missed"]:
        print(f"紧急诉求兜底：被判紧急、转人工或低置信送复核的 {u['caught']}/{u['caught'] + u['missed']}"
              "（低置信送复核规则是看过留出集错例后加的，此项在留出集上偏乐观）")
    print("混淆矩阵（行=标注，列=预测）：" + " ".join(f"{c[:2]:>4}" for c in CATEGORIES))
    for g, row in r["confusion"].items():
        print(f"  {g:<8}" + " ".join(f"{row[p]:>4}" for p in CATEGORIES))
    for e in r["errors"]:
        print(f"  ✗ {e['text']}\n      错在 {e['wrong']}：标注 {e['gold']} / 预测 {e['pred']}")
    return r


if __name__ == "__main__":
    paths = [Path(p) for p in sys.argv[1:]] or [p for p in DEFAULT if p.exists()]
    for p in paths:
        report(p)
