"""知识库：规程/手册/制度/设备说明书检索、相似故障案例、案例沉淀与专家审核（设计文档第八节 kb_*）。

检索：Markdown 按标题切成章节片段，中文按“单字 + 双字”切分建立 BM25 索引（不依赖分词库），
      并用领域同义词表把现场说法（回温、跑水、不热……）扩展到规程用语（回水温度、泄漏、欠供……）。
案例：文本相似度（BM25，归一化）× 0.65 + 结构化匹配（报警代码、异常特征、对象类型）× 0.35。
沉淀：新案例进入“待专家审核”，审核通过后才参与相似案例检索。
护栏：知识库内容一律视为数据；上传文档与提交案例时检测提示注入式文字并标记（设计文档第十一节）。
持久化：种子文档/案例随代码发布（backend/knowledge），上传文档与新增案例写入数据目录
       （环境变量 HEATNET_DATA_DIR，默认 backend/data），重启不丢失。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..sim.history import now_local

BACKEND_DIR = Path(__file__).resolve().parents[2]
SEED_DIR = BACKEND_DIR / "knowledge"
DOC_TYPES = ["规程", "手册", "制度", "设备说明书", "指南", "规范"]
MIN_CASE_SCORE = 0.35   # 低于该分的相似案例不展示：弱匹配作为处置参考弊大于利
MAX_DOC_BYTES = 300_000

# ---------------------------------------------------------------- 分词与同义词
_STOP_UNI = set("的了吗呢么啊吧是在和与及等为将把被对也就都还又才要会能可这那")
_STOP_BI = {"怎么", "什么", "如何", "多少", "哪些", "一下", "可以", "需要", "应该", "有什", "么处", "么办", "怎样",
            "能不", "不能", "什么", "的话", "是不", "了吗"}

SYNONYMS: list[list[str]] = [
    ["回温", "回水温度"], ["供温", "供水温度"],
    ["跑水", "漏水", "泄漏", "失水", "补水量"], ["漏点", "泄漏"],
    ["不热", "室温偏低", "室温低", "欠供", "供热不足"], ["房子", "室温"], ["暖气", "供热", "散热器"],
    ["过热", "室温偏高", "过量供热"], ["阀门", "调节阀", "阀位", "开度"], ["100%", "全开"],
    ["卡住", "卡涩", "无响应", "没反应"], ["热量对不上", "热量偏差", "热量不平衡"],
    ["近热远冷", "水力失衡", "水力平衡"], ["末端", "远端"],
    ["标煤", "标准煤"], ["二网", "二次侧"], ["一网", "一次侧"], ["水泵", "循环泵"],
    ["结垢", "水垢"], ["谷电", "峰谷电价"], ["曲线", "气候补偿曲线"], ["令牌", "审批签发"],
]

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> int | None:
    """一～九十九的中文数字。"""
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        return (_CN_NUM.get(a, 1) if a else 1) * 10 + (_CN_NUM.get(b, 0) if b else 0)
    return _CN_NUM.get(s)


_BELOW_ZERO = re.compile(r"零下\s*([0-9一二两三四五六七八九十]{1,3})\s*(度|°c)")   # NFKC 之后 ℃ 已变为 °c


def normalize(text: str) -> str:
    """全角转半角、小写（NFKC 会把 ℃ 变成 °c），并把“零下十度”这类口语温度统一成“-10°c”。"""
    t = unicodedata.normalize("NFKC", text).lower()
    return _BELOW_ZERO.sub(lambda m: f"-{_cn_to_int(m.group(1))}°c" if _cn_to_int(m.group(1)) is not None else m.group(0), t)


_CJK = re.compile(r"[一-鿿]+")
_TOKEN = re.compile(r"[一-鿿]+|[a-z0-9_%.\-℃]+")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN.finditer(normalize(text)):
        s = m.group()
        if _CJK.fullmatch(s):
            out += [c for c in s if c not in _STOP_UNI]
            out += [s[i:i + 2] for i in range(len(s) - 1) if s[i:i + 2] not in _STOP_BI]
        else:
            s = s.strip(".").rstrip("-")
            if s and not (len(s) == 1 and s.isalpha()):     # 丢弃 “°c” 残留的 “c” 等单字母
                out.append(s)
    return out


def expand_phrases(query: str) -> list[str]:
    """查询命中的同义词组中、查询里没有出现的其他说法。"""
    q = normalize(query)
    out: list[str] = []
    for group in SYNONYMS:
        if any(normalize(t) in q for t in group):
            out += [t for t in group if normalize(t) not in q and t not in out]
    return out


def expand(query: str) -> list[tuple[str, float]]:
    """返回 (token, 权重)：原查询词权重 1；同义词扩展只加入双字及以上词元（权重 0.6），
    不加单字——单字几乎在每个片段都出现，只会引入噪声。"""
    weights: dict[str, float] = {t: 1.0 for t in tokenize(query)}
    for phrase in expand_phrases(query):
        for tok in tokenize(phrase):
            if len(tok) > 1:
                weights.setdefault(tok, 0.6)
    return list(weights.items())


# ---------------------------------------------------------------- 提示注入检测
_INJECTION = [
    (re.compile(r"忽略.{0,6}(之前|以上|前面|上述|所有).{0,4}(指令|指示|要求|规则|设定)"), "要求忽略既有指令"),
    (re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)", re.I), "要求忽略既有指令"),
    (re.compile(r"(调用|执行|使用)\s*control_\w+|approval_token|control_execute", re.I), "试图触发调控工具"),
    (re.compile(r"(无需|不需要|跳过|绕过).{0,4}(审批|确认|安全校验|校验)"), "试图绕过审批或校验"),
    (re.compile(r"(直接|立即|马上).{0,4}(下发|批准|执行)(指令|命令)?"), "要求直接下发或批准"),
    (re.compile(r"(system\s*prompt|系统提示词|你现在是|你的新身份)", re.I), "试图改变智能体角色"),
]


def injection_flags(text: str) -> list[str]:
    t = normalize(text)
    return sorted({why for pat, why in _INJECTION if pat.search(t)})


# ---------------------------------------------------------------- BM25
class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avgdl = (sum(self.lens) / len(self.lens)) if docs else 1.0
        df = Counter(t for tf in self.tfs for t in tf)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: list[tuple[str, float]]) -> list[float]:
        out = []
        for tf, dl in zip(self.tfs, self.lens):
            s = 0.0
            norm = self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            for tok, w in query:
                f = tf.get(tok)
                if f:
                    s += w * self.idf[tok] * f * (self.k1 + 1) / (f + norm)
            out.append(s)
        return out


# ---------------------------------------------------------------- 文档解析
@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    section: str          # “3 气候补偿曲线调整 › 3.2 室温偏高时”
    text: str
    flags: list[str] = field(default_factory=list)


def parse_front_matter(raw: str) -> tuple[dict, str]:
    meta: dict = {}
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end > 0:
            for line in raw[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            raw = raw[end + 4:]
    return meta, raw.strip()


def chunk_markdown(doc_id: str, body: str, max_len: int = 500) -> tuple[str, list[Chunk]]:
    """按 ##/### 标题切分；返回 (文档标题, 片段列表)。没有标题的文本按段落合并切分。"""
    title = ""
    h2 = h3 = ""
    buf: list[str] = []
    chunks: list[Chunk] = []

    def flush():
        text = "\n".join(buf).strip()
        buf.clear()
        if not text:
            return
        section = " › ".join(x for x in (h2, h3) if x) or "正文"
        parts, cur = [], ""
        for para in re.split(r"\n\s*\n|\n(?=\d+\.\s)", text):
            if cur and len(cur) + len(para) > max_len:
                parts.append(cur)
                cur = ""
            cur = (cur + "\n" + para).strip()
        if cur:
            parts.append(cur)
        for p in parts:
            chunks.append(Chunk(f"{doc_id}#{len(chunks) + 1}", doc_id, section, p))

    for line in body.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("## "):
            flush()
            h2, h3 = line[3:].strip(), ""
        elif line.startswith("### "):
            flush()
            h3 = line[4:].strip()
        else:
            buf.append(line)
    flush()
    return title, chunks


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class KBError(ValueError):
    pass


# ---------------------------------------------------------------- 知识库
class KnowledgeBase:
    def __init__(self, data_dir: str | os.PathLike | None = None, seed_dir: Path = SEED_DIR):
        self.seed_dir = Path(seed_dir)
        self.data_dir = Path(data_dir or os.environ.get("HEATNET_DATA_DIR") or BACKEND_DIR / "data")
        self.lock = threading.RLock()
        self.docs: dict[str, dict] = {}
        self.chunks: list[Chunk] = []
        self.cases: list[dict] = []
        self._load()

    # ---------- 加载 ----------
    def _load(self):
        for path in sorted((self.seed_dir / "docs").glob("*.md")):
            self._add_doc_text(path.read_text(encoding="utf-8"), source="seed")
        up = self.data_dir / "kb_docs"
        if up.exists():
            for path in sorted(up.glob("*.md")):
                self._add_doc_text(path.read_text(encoding="utf-8"), source="upload")
        seed_cases = json.loads((self.seed_dir / "cases.json").read_text(encoding="utf-8"))
        for c in seed_cases:
            c.setdefault("source", "seed")
            c.setdefault("flags", [])
        user_cases_path = self.data_dir / "kb_cases.json"
        user_cases = json.loads(user_cases_path.read_text(encoding="utf-8")) if user_cases_path.exists() else []
        self.cases = seed_cases + user_cases
        self._reindex()

    def _add_doc_text(self, raw: str, source: str) -> dict:
        meta, body = parse_front_matter(raw)
        doc_id = meta.get("id") or f"UP-{uuid.uuid4().hex[:6].upper()}"
        title, chunks = chunk_markdown(doc_id, body)
        for ch in chunks:
            ch.flags = injection_flags(ch.text)
        doc = {"doc_id": doc_id, "title": meta.get("title") or title or doc_id, "type": meta.get("type", "指南"),
               "version": meta.get("version", ""), "updated": meta.get("updated", ""), "owner": meta.get("owner", ""),
               "sample": meta.get("sample", "false") == "true", "source": source,
               "uploaded_by": meta.get("uploaded_by", ""), "body": body,
               "sections": list(dict.fromkeys(c.section for c in chunks)), "n_chunks": len(chunks),
               "flags": sorted({f for c in chunks for f in c.flags})}
        self.docs[doc_id] = doc
        self.chunks = [c for c in self.chunks if c.doc_id != doc_id] + chunks
        return doc

    def _reindex(self):
        def doc_text(c: Chunk):
            d = self.docs[c.doc_id]
            return f"{d['title']} {c.section} {c.section} {c.text}"   # 标题与章节名加权
        self.chunk_index = BM25([tokenize(doc_text(c)) for c in self.chunks])
        self.case_index = BM25([tokenize(f"{c['title']} {c['symptom']} {c['symptom']} {c.get('cause', '')} "
                                         f"{' '.join(c.get('tags', []))}") for c in self.cases])

    # ---------- 检索 ----------
    def search(self, query: str, doc_types: list[str] | None = None, top_k: int = 5, use_synonyms: bool = True) -> dict:
        query = (query or "").strip()
        if not query:
            raise KBError("检索词不能为空")
        q = expand(query) if use_synonyms else [(t, 1.0) for t in tokenize(query)]
        with self.lock:
            scores = self.chunk_index.scores(q)
            ranked = sorted(range(len(self.chunks)), key=lambda i: -scores[i])
            results = []
            for i in ranked:
                if scores[i] <= 0 or len(results) >= top_k:
                    break
                c = self.chunks[i]
                d = self.docs[c.doc_id]
                if doc_types and d["type"] not in doc_types:
                    continue
                snippet, hl = self._snippet(c.text, q)
                results.append({"chunk_id": c.chunk_id, "doc_id": c.doc_id, "title": d["title"], "type": d["type"],
                                "section": c.section, "snippet": snippet, "highlights": hl,
                                "score": round(scores[i], 3), "updated": d["updated"], "version": d["version"],
                                "sample": d["sample"], "flags": c.flags})
        expanded = expand_phrases(query) if use_synonyms else []
        return {"query": query, "results": results, "expanded_terms": expanded[:12], "total_chunks": len(self.chunks)}

    @staticmethod
    def _snippet(text: str, q: list[tuple[str, float]], width: int = 160) -> tuple[str, list[str]]:
        terms = [t for t, _ in q if len(t) > 1]
        sents = [s for s in re.split(r"(?<=[。；！？\n])", text) if s.strip()]
        best, best_score = 0, -1.0
        for i, s in enumerate(sents):
            ns = normalize(s)
            sc = sum(w for t, w in q if len(t) > 1 and t in ns)
            if sc > best_score:
                best, best_score = i, sc
        snip = sents[best].strip() if sents else text
        j = best + 1
        while j < len(sents) and len(snip) + len(sents[j]) <= width:
            snip += sents[j].strip()
            j += 1
        if len(snip) > width:
            snip = snip[:width] + "…"
        hl = sorted({t for t in terms if t in normalize(snip)}, key=len, reverse=True)
        return snip, hl[:10]

    def get_doc(self, doc_id: str) -> dict:
        d = self.docs.get(doc_id)
        if not d:
            raise KBError(f"文档 {doc_id} 不存在")
        return d

    def list_docs(self) -> list[dict]:
        keys = ("doc_id", "title", "type", "version", "updated", "owner", "sample", "source", "uploaded_by",
                "n_chunks", "sections", "flags")
        return sorted(({k: d[k] for k in keys} for d in self.docs.values()), key=lambda d: d["doc_id"])

    # ---------- 文档管理 ----------
    def add_document(self, content: str, *, title: str, doc_type: str, uploaded_by: str, version: str = "") -> dict:
        if doc_type not in DOC_TYPES:
            raise KBError(f"文档类型须为 {DOC_TYPES}")
        if len(content.encode("utf-8")) > MAX_DOC_BYTES:
            raise KBError("文档超过 300KB，请拆分后上传")
        _, body = parse_front_matter(content)
        if len(body.strip()) < 20:
            raise KBError("文档内容过短")
        if not re.search(r"^#{1,3} ", body, re.M):
            body = f"# {title}\n\n## 正文\n{body}"
        doc_id = f"UP-{uuid.uuid4().hex[:6].upper()}"
        today = now_local().strftime("%Y-%m-%d")
        raw = (f"---\nid: {doc_id}\ntitle: {title}\ntype: {doc_type}\nversion: {version}\nupdated: {today}\n"
               f"owner: \nuploaded_by: {uploaded_by}\nsample: false\n---\n{body}\n")
        with self.lock:
            _atomic_write(self.data_dir / "kb_docs" / f"{doc_id}.md", raw)
            doc = self._add_doc_text(raw, source="upload")
            self._reindex()
        return {k: doc[k] for k in ("doc_id", "title", "type", "n_chunks", "sections", "flags")}

    def delete_document(self, doc_id: str) -> dict:
        with self.lock:
            d = self.get_doc(doc_id)
            if d["source"] != "upload":
                raise KBError("随系统发布的种子文档不能在线删除，请在代码仓库 backend/knowledge/docs 中维护")
            (self.data_dir / "kb_docs" / f"{doc_id}.md").unlink(missing_ok=True)
            del self.docs[doc_id]
            self.chunks = [c for c in self.chunks if c.doc_id != doc_id]
            self._reindex()
        return {"doc_id": doc_id, "deleted": True}

    # ---------- 案例 ----------
    def similar_cases(self, symptom: str, *, object_type: str | None = None, alarm_codes: list[str] | None = None,
                      features: list[str] | None = None, top_k: int = 3, include_pending: bool = False) -> dict:
        if not (symptom or alarm_codes or features):
            raise KBError("需要症状描述、报警代码或异常特征")
        codes, feats = set(alarm_codes or []), set(features or [])
        with self.lock:
            text_scores = self.case_index.scores(expand(symptom)) if symptom else [0.0] * len(self.cases)
            mx = max(text_scores) if text_scores and max(text_scores) > 0 else 1.0
            out = []
            for c, ts in zip(self.cases, text_scores):
                if c["status"] != "approved" and not (include_pending and c["status"] == "pending_review"):
                    continue
                cc, cf = set(c.get("alarm_codes", [])), set(c.get("features", []))
                # 覆盖率：查询给出的报警代码/特征有多少出现在案例中（案例多出的信号不扣分）
                parts = []
                if codes:
                    parts.append((0.5, len(codes & cc) / len(codes)))
                if feats:
                    parts.append((0.3, len(feats & cf) / len(feats)))
                if object_type:
                    parts.append((0.2, 1.0 if c.get("object_type") == object_type else 0.0))
                struct = sum(w * v for w, v in parts) / sum(w for w, _ in parts) if parts else None
                text = ts / mx if symptom else None
                # 只按查询实际提供的信号加权：都有时 文本 0.65 + 结构化 0.35
                if text is not None and struct is not None:
                    score = 0.65 * text + 0.35 * struct
                else:
                    score = text if text is not None else struct
                text = text or 0.0
                ot = 1.0 if object_type and c.get("object_type") == object_type else 0.0
                reasons = []
                if text > 0.3:
                    reasons.append(f"症状描述相似度 {text:.2f}")
                if codes & cc:
                    reasons.append("报警代码相同：" + "、".join(sorted(codes & cc)))
                if feats & cf:
                    reasons.append("异常特征相同：" + "、".join(sorted(feats & cf)))
                if ot:
                    reasons.append("对象类型一致")
                out.append((score, reasons, c))
        out.sort(key=lambda x: -x[0])
        res = [{**{k: c[k] for k in ("case_id", "title", "object_type", "object_name", "symptom", "cause", "steps",
                                       "outcome", "handler", "occurred_at", "status", "alarm_codes", "features",
                                       "tags", "flags") if k in c},
                "score": round(s, 3), "match_reasons": r}
               for s, r, c in out[:top_k] if s >= MIN_CASE_SCORE]
        return {"cases": res, "searched": len(out)}

    def list_cases(self, status: str | None = None) -> list[dict]:
        rows = [c for c in self.cases if status in (None, "", c["status"])]
        return sorted(rows, key=lambda c: c.get("submitted_at") or c.get("occurred_at", ""), reverse=True)

    def get_case(self, case_id: str) -> dict:
        c = next((c for c in self.cases if c["case_id"] == case_id), None)
        if not c:
            raise KBError(f"案例 {case_id} 不存在")
        return c

    def _persist_cases(self):
        _atomic_write(self.data_dir / "kb_cases.json",
                      json.dumps([c for c in self.cases if c.get("source") != "seed"], ensure_ascii=False, indent=1))

    def save_case(self, *, title: str, symptom: str, steps: list[str], outcome: str, cause: str = "",
                  object_type: str = "station", object_name: str = "", alarm_codes: list[str] | None = None,
                  features: list[str] | None = None, event: dict | None = None, submitted_by: str,
                  tags: list[str] | None = None) -> dict:
        steps = [s.strip() for s in steps if s and s.strip()]
        if not title.strip() or not symptom.strip() or not steps or not outcome.strip():
            raise KBError("案例须包含标题、现象、至少一个处置步骤和处置结果")
        if len(steps) > 20 or any(len(s) > 300 for s in steps):
            raise KBError("处置步骤最多 20 条，每条不超过 300 字")
        flags = injection_flags(" ".join([title, symptom, cause, outcome, *steps]))
        now = now_local()
        case = {"case_id": f"CASE-U{uuid.uuid4().hex[:6].upper()}", "title": title.strip()[:80],
                "object_type": object_type, "object_name": object_name, "symptom": symptom.strip()[:500],
                "alarm_codes": alarm_codes or [], "features": features or [], "cause": cause.strip()[:300],
                "steps": steps, "outcome": outcome.strip()[:300], "handler": submitted_by,
                "occurred_at": (event or {}).get("occurred_at") or now.strftime("%Y-%m-%d"),
                "status": "pending_review", "submitted_by": submitted_by,
                "submitted_at": now.isoformat(timespec="seconds"), "event": event, "tags": tags or [],
                "flags": flags, "source": "user"}
        with self.lock:
            self.cases.append(case)
            self._persist_cases()
            self._reindex()
        return case

    def review_case(self, case_id: str, approve: bool, reviewer: str, comment: str = "") -> dict:
        with self.lock:
            c = self.get_case(case_id)
            if c["status"] != "pending_review":
                raise KBError(f"案例状态为 {c['status']}，不能重复审核")
            if c.get("submitted_by") == reviewer:
                raise KBError("提交人不能审核自己提交的案例")
            c["status"] = "approved" if approve else "rejected"
            c["reviewed_by"] = reviewer
            c["review_comment"] = comment
            c["reviewed_at"] = now_local().isoformat(timespec="seconds")
            self._persist_cases()
            self._reindex()
        return c

    def stats(self) -> dict:
        st = Counter(c["status"] for c in self.cases)
        return {"docs": len(self.docs), "chunks": len(self.chunks), "cases_approved": st.get("approved", 0),
                "cases_pending": st.get("pending_review", 0), "doc_types": DOC_TYPES}

    # ---------- 评测 ----------
    def evaluate(self, queries: list[dict], use_synonyms: bool = True, k: int = 3) -> dict:
        hit1 = hitk = 0
        rr = 0.0
        rows = []
        for item in queries:
            gold = [(item["doc"], item["section"])] + [tuple(a) for a in item.get("alt", [])]
            res = self.search(item["q"], top_k=10, use_synonyms=use_synonyms)["results"]
            rank = next((i + 1 for i, r in enumerate(res) if any(r["doc_id"] == d and s in r["section"] for d, s in gold)), None)
            hit1 += rank == 1
            hitk += rank is not None and rank <= k
            rr += 1 / rank if rank else 0
            rows.append({"q": item["q"], "rank": rank, "top": f"{res[0]['doc_id']} {res[0]['section']}" if res else None})
        n = len(queries)
        return {"n": n, "hit@1": round(hit1 / n, 3), f"hit@{k}": round(hitk / n, 3), "mrr": round(rr / n, 3), "rows": rows}
