"""中文文本向量模型（知识库语义检索用）。

默认模型 BAAI/bge-small-zh-v1.5：中文专用、512 维、约 90MB，经 fastembed 用 ONNX Runtime 在 CPU 上推理，
不需要 PyTorch。模型文件首次使用时从 HuggingFace 下载到缓存目录；内网/离线部署时把模型目录拷到服务器，
用 HEATNET_EMBED_PATH 指向它即可，不再联网。

环境变量
  HEATNET_EMBED          auto（默认，加载失败则退回关键词检索）| off（关闭）| required（加载失败即报错）
  HEATNET_EMBED_MODEL    模型名，默认 BAAI/bge-small-zh-v1.5（fastembed 支持的其他中文/多语模型亦可）
  HEATNET_EMBED_PATH     本地 ONNX 模型目录（离线部署）
  HEATNET_EMBED_CACHE    下载缓存目录，默认 backend/models
  HEATNET_EMBED_QUERY_INSTRUCTION  查询前缀；BGE 中文模型默认“为这个句子生成表示以用于检索相关文章：”，设为空串可关闭
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger("heatnet.embedding")

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
BGE_ZH_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class Embedder(Protocol):
    name: str
    dim: int

    def embed_docs(self, texts: list[str]) -> np.ndarray: ...      # (n, dim)，已 L2 归一化

    def embed_query(self, text: str) -> np.ndarray: ...            # (dim,)，已 L2 归一化


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.maximum(n, 1e-12)


class FastEmbedder:
    """fastembed（ONNX）封装。"""

    def __init__(self, model: str = DEFAULT_MODEL, *, cache_dir: str | None = None, local_path: str | None = None,
                 query_instruction: str | None = None, threads: int | None = None):
        from fastembed import TextEmbedding   # 延迟导入：未安装时由 load_embedder 处理

        kw: dict = {}
        if local_path:
            kw.update(specific_model_path=local_path, local_files_only=True)
        self.model = TextEmbedding(model_name=model, cache_dir=cache_dir, threads=threads, **kw)
        self.name = model
        if query_instruction is None:
            query_instruction = BGE_ZH_INSTRUCTION if "bge" in model.lower() and "zh" in model.lower() else ""
        self.query_instruction = query_instruction
        self.dim = int(self.embed_query("测试").shape[0])

    def embed_docs(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, getattr(self, "dim", 0)), dtype=np.float32)
        return _normalize(np.stack(list(self.model.passage_embed(texts, batch_size=32))))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalize(np.asarray(next(iter(self.model.query_embed(self.query_instruction + text)))))


def load_embedder() -> tuple[Embedder | None, dict]:
    """返回 (向量模型或 None, 状态)。auto 模式下任何失败都只记录原因，知识库退回关键词检索。"""
    mode = os.environ.get("HEATNET_EMBED", "auto").lower()
    model = os.environ.get("HEATNET_EMBED_MODEL", DEFAULT_MODEL)
    status = {"enabled": False, "model": model, "dim": None, "reason": ""}
    if mode in ("off", "0", "false", "none"):
        status["reason"] = "已通过 HEATNET_EMBED=off 关闭向量检索"
        return None, status
    try:
        import fastembed  # noqa: F401
    except ImportError:
        status["reason"] = "未安装 fastembed（pip install fastembed）"
        if mode == "required":
            raise
        return None, status
    try:
        instr = os.environ.get("HEATNET_EMBED_QUERY_INSTRUCTION")
        emb = FastEmbedder(model, cache_dir=os.environ.get("HEATNET_EMBED_CACHE") or str(BACKEND_DIR / "models"),
                           local_path=os.environ.get("HEATNET_EMBED_PATH") or None, query_instruction=instr)
    except Exception as e:  # noqa: BLE001 —— 下载被拦截、文件缺失、ONNX 错误等都在这里
        msg = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
        status["reason"] = f"向量模型加载失败：{msg}"
        if "any source" in msg or "403" in msg or "download" in msg.lower():
            status["reason"] += "（无法下载模型：请放通 huggingface.co，或离线拷入模型目录并设置 HEATNET_EMBED_PATH）"
        log.warning(status["reason"])
        if mode == "required":
            raise
        return None, status
    status.update(enabled=True, dim=emb.dim, reason="", query_instruction=emb.query_instruction)
    return emb, status
