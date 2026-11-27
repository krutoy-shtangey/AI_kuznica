"""Offline Sentence Transformers adapter. Model files are provisioned separately."""
import os
import re
from pathlib import Path

import numpy as np

MODEL = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_ROOT = Path(os.environ.get("RETRIEVAL_MODEL_ROOT") or Path(__file__).resolve().parents[1] / "models")


class Encoder:
    def __init__(self, model=MODEL, revision=REVISION, batch_size=16, device="cpu",
                 model_root=MODEL_ROOT):
        if batch_size < 1 or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Expected a positive batch size and immutable model revision")
        if not re.fullmatch(r"[\w-]+/[\w.-]+", model) or model.endswith("/.."):
            raise ValueError("Expected a model repository ID")
        self.model, self.revision = model, revision
        self.batch_size, self.device = batch_size, device
        path = Path(model_root) / model / revision
        os.environ.update(HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_OFFLINE="1",
                          TRANSFORMERS_OFFLINE="1", DO_NOT_TRACK="1")
        try:
            from sentence_transformers import SentenceTransformer
            if not path.is_dir():
                raise FileNotFoundError
            self.network = SentenceTransformer(str(path.resolve()), device=device,
                                               local_files_only=True, trust_remote_code=False,
                                               model_kwargs={"use_safetensors": True})
        except Exception as exc:
            raise RuntimeError("Local embedding model unavailable") from exc
        self.tokenizer = self.network.tokenizer
        self.dimension = self.network.get_sentence_embedding_dimension()
        self.limit = min(512, self.network.max_seq_length, self.tokenizer.model_max_length)
        if not self.tokenizer.is_fast:
            raise ValueError("A fast tokenizer with source offsets is required")

    def count(self, text):
        return len(self.tokenizer(text, truncation=False, verbose=False)["input_ids"])

    def offsets(self, text):
        return self.tokenizer(text, add_special_tokens=False, truncation=False, verbose=False,
                              return_offsets_mapping=True)["offset_mapping"]

    def encode(self, inputs):
        if any(self.count(text) > self.limit for text in inputs):
            raise ValueError("Embedding input exceeds model limit")
        if not inputs:
            return np.empty((0, self.dimension), dtype=np.float32)
        return self.network.encode(inputs, batch_size=self.batch_size, prompt="",
                                   normalize_embeddings=True, convert_to_numpy=True,
                                   show_progress_bar=False).astype(np.float32, copy=False)

    def encode_queries(self, queries):
        return self.encode(["query: " + query for query in queries])
