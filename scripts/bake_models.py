"""Download the sentence-transformers models into the image at build time.

Run only by the `Dockerfile` builder stage, with `HF_HOME` pointed at the
directory that is copied into the runtime image. See the Dockerfile comment
for why (the non-root runtime user has no writable `$HOME`, and a first-run
HuggingFace download is rate-limited).

The model ids come from the app's own constants so a model change in one
place can't leave the image baking the wrong weights.
"""

from __future__ import annotations

from sentence_transformers import CrossEncoder, SentenceTransformer

from app.agents.retrieval.reranking import _MODEL_NAME as RERANKER_MODEL
from app.retrieval.embedding import _MODEL_NAME as EMBEDDING_MODEL

SentenceTransformer(EMBEDDING_MODEL)
CrossEncoder(RERANKER_MODEL)
print(f"baked: {EMBEDDING_MODEL}, {RERANKER_MODEL}")
