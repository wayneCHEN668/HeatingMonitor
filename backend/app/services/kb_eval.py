"""知识库检索评测：在开发集与留出集上比较 关键词 / 向量 / 混合 检索。

  cd backend && python -m app.services.kb_eval            # 打印 Markdown 表格
  python -m app.services.kb_eval --json result.json        # 同时输出明细

开发集 eval_queries.json（30 条）是构建检索时就有的问题，同义词表在它上面形成，结果偏乐观；
留出集 eval_queries_paraphrase.json（20 条，改写式、与文档字面重合少）在实现向量检索之前写定并提交，
用于公平比较关键词与语义检索。向量模型不可用时只输出关键词结果并说明原因，不会用替代数据填充。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time

from .embedding import BGE_ZH_INSTRUCTION, load_embedder
from .kb import SEED_DIR, KnowledgeBase

SETS = {"开发集": "eval_queries.json", "留出集（改写式）": "eval_queries_paraphrase.json"}


def run(json_out: str | None = None) -> dict:
    emb, status = load_embedder()
    kb = KnowledgeBase(data_dir=tempfile.mkdtemp(prefix="kb-eval-"), embedder=emb, embed_status=status)
    configs = [("BM25", "bm25", False), ("BM25 + 同义词", "bm25", True)]
    if emb is not None:
        configs += [("向量", "vector", False), ("混合 RRF", "hybrid", False), ("混合 RRF + 同义词", "hybrid", True)]
    out = {"retrieval": status, "results": {}}
    for set_name, fname in SETS.items():
        queries = json.loads((SEED_DIR / fname).read_text(encoding="utf-8"))
        out["results"][set_name] = {}
        for label, mode, syn in configs:
            t = time.perf_counter()
            r = kb.evaluate(queries, use_synonyms=syn, mode=mode)
            r["ms_per_query"] = round((time.perf_counter() - t) * 1000 / len(queries), 1)
            out["results"][set_name][label] = r
        # 查询指令消融（只对 BGE 中文模型有意义）
        if emb is not None and getattr(emb, "query_instruction", ""):
            saved = emb.query_instruction
            emb.query_instruction = ""
            r = kb.evaluate(queries, use_synonyms=False, mode="vector")
            emb.query_instruction = saved
            out["results"][set_name]["向量（无查询指令）"] = r

    print(f"# 知识库检索评测\n\n向量模型：{status['model'] if status['enabled'] else '未启用'}"
          + ("" if status["enabled"] else f"（{status['reason']}）"))
    if emb is not None and getattr(emb, "query_instruction", ""):
        print(f"查询指令：{BGE_ZH_INSTRUCTION}")
    for set_name, rows in out["results"].items():
        n = next(iter(rows.values()))["n"]
        print(f"\n## {set_name}（{n} 条）\n\n| 方案 | hit@1 | hit@3 | MRR | ms/查询 |\n|---|---|---|---|---|")
        for label, r in rows.items():
            print(f"| {label} | {r['hit@1']:.3f} | {r['hit@3']:.3f} | {r['mrr']:.3f} | {r.get('ms_per_query', '-')} |")
    if not status["enabled"]:
        print("\n> 向量相关结果缺失：模型未加载。放通 huggingface.co 或设置 HEATNET_EMBED_PATH 后重新运行。")
    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", help="明细输出文件")
    a = ap.parse_args()
    run(a.json)
    sys.exit(0)
