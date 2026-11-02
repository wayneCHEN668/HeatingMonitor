"""向量检索与混合融合测试。

ConceptEmbedder 是测试替身：把人为指定的关键词映射到同一维度（如“石子/汽蚀/抽空”→“汽蚀”维），
用来确定性地验证管线（融合、缓存、挂接、案例语义分），**不代表任何真实模型的效果**。
真实中文模型（bge-small-zh-v1.5）的测试需设置 HEATNET_TEST_REAL_EMBED=1 且模型可用时才运行。
"""
import json
import os
import tempfile

import numpy as np
import pytest

from app.services.kb import SEED_DIR, KBError, KnowledgeBase

CONCEPTS = {
    "汽蚀": ["汽蚀", "石子", "抽空"],
    "泄漏": ["泄漏", "跑水", "漏水", "失水"],
    "结垢": ["结垢", "水垢"],
    "卡涩": ["卡涩", "动不了", "卡死"],
}


class ConceptEmbedder:
    name = "test/concept-embedder"

    def __init__(self):
        self.dim = len(CONCEPTS) + 1
        self.doc_calls = 0
        self.docs_embedded = 0

    def _vec(self, text: str) -> np.ndarray:
        v = np.array([sum(text.count(w) for w in ws) for ws in CONCEPTS.values()] + [0.05], dtype=np.float32)
        return v / np.linalg.norm(v)

    def embed_docs(self, texts):
        self.doc_calls += 1
        self.docs_embedded += len(texts)
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


def make_kb(data=None, emb=None):
    emb = emb or ConceptEmbedder()
    return KnowledgeBase(data_dir=data or tempfile.mkdtemp(prefix="kbv-"), embedder=emb,
                         embed_status={"enabled": True, "model": emb.name, "dim": emb.dim, "reason": ""}), emb


def test_hybrid_surfaces_semantic_only_match():
    kb, _ = make_kb()
    q = "水泵里面像有石子在滚"                       # 与“汽蚀”章节几乎没有字面重合
    vec = kb.search(q, mode="vector", top_k=3)["results"]
    assert vec[0]["doc_id"] == "EQ-02" and "汽蚀" in vec[0]["section"] and vec[0]["match"] == "语义"
    hyb = kb.search(q, top_k=5)                       # 默认混合
    assert hyb["mode"] == "hybrid"
    hit = next(r for r in hyb["results"] if r["doc_id"] == "EQ-02" and "汽蚀" in r["section"])
    assert hit["vec_rank"] == 1 and "语义" in hit["match"]
    # 关键词能命中的问题，融合后仍然命中（向量不应把它挤掉）
    r = kb.search("燃气泄漏应急处置", top_k=3)["results"]
    assert r[0]["doc_id"] == "SOP-03" and "燃气泄漏" in r[0]["section"]


def test_rrf_scores_and_fields():
    kb, _ = make_kb()
    r = kb.search("跑水怎么查漏点", top_k=5)["results"]
    both = [x for x in r if x["match"] == "关键词+语义"]
    assert both, "两路都命中的片段应标为 关键词+语义"
    x = both[0]
    assert abs(x["score"] - (1 / (60 + x["bm25_rank"]) + 1 / (60 + x["vec_rank"]))) < 1e-3
    assert all(r[i]["score"] >= r[i + 1]["score"] for i in range(len(r) - 1))


def test_bm25_only_without_embedder():
    kb = KnowledgeBase(data_dir=tempfile.mkdtemp(prefix="kbv-"))
    assert kb.mode == "bm25" and kb.stats()["retrieval"]["mode"] == "bm25"
    assert kb.search("阀门卡涩")["mode"] == "bm25"
    with pytest.raises(KBError, match="向量模型未加载"):
        kb.search("阀门卡涩", mode="vector")


def test_attach_embedder_switches_mode():
    kb = KnowledgeBase(data_dir=tempfile.mkdtemp(prefix="kbv-"))
    emb = ConceptEmbedder()
    kb.attach_embedder(emb, {"enabled": True, "model": emb.name, "dim": emb.dim, "reason": ""})
    st = kb.stats()["retrieval"]
    assert st["mode"] == "hybrid" and st["vectors"] == len(kb.chunks) + len(kb.cases)


def test_vector_cache_avoids_recompute():
    data = tempfile.mkdtemp(prefix="kbv-")
    kb, emb1 = make_kb(data)
    n_first = emb1.docs_embedded
    assert n_first == len(kb.chunks) + len(kb.cases)
    kb2, emb2 = make_kb(data)                         # 重启：全部命中磁盘缓存
    assert emb2.docs_embedded == 0
    kb2.add_document("## 一\n这是一份关于水垢清洗周期的补充说明，每年采暖季前检查一次。", title="补充说明",
                     doc_type="指南", uploaded_by="测试")
    assert emb2.docs_embedded == 1                    # 只向量化新增片段
    assert kb2.search("设备里有水垢", mode="vector", top_k=5)["results"]


def test_similar_cases_use_semantics():
    kb, _ = make_kb()
    r = kb.similar_cases("循环泵像有石子在里面响", object_type="station")
    assert r["mode"] == "hybrid" and r["cases"][0]["case_id"] == "CASE-0006"     # 失压汽蚀案例
    assert "关键词+语义" in r["cases"][0]["match_reasons"][0]


@pytest.mark.skipif(os.environ.get("HEATNET_TEST_REAL_EMBED") != "1",
                    reason="真实向量模型测试需 HEATNET_TEST_REAL_EMBED=1 且能加载 bge-small-zh-v1.5")
def test_real_chinese_model_pipeline():
    from app.services.embedding import load_embedder
    os.environ["HEATNET_EMBED"] = "required"
    emb, status = load_embedder()
    assert status["enabled"] and status["dim"] == 512
    kb = KnowledgeBase(data_dir=tempfile.mkdtemp(prefix="kbv-"), embedder=emb, embed_status=status)
    held = json.loads((SEED_DIR / "eval_queries_paraphrase.json").read_text(encoding="utf-8"))
    for mode in ("bm25", "vector", "hybrid"):
        r = kb.evaluate(held, mode=mode)
        print(mode, {k: v for k, v in r.items() if k != "rows"})
    assert kb.search("水泵声音很大像有石子", mode="vector")["results"]
