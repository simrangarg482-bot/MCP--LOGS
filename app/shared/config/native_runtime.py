"""Bound the native (C/Fortran) math thread pools that NumPy, PyTorch and
`sentence-transformers` pull in transitively.

Owned by: shared/ -- cross-cutting, no business meaning of its own.

**Why this exists.** OpenBLAS/MKL/OpenMP each default their thread count to
the machine's CPU count (12 here) and *preallocate* per-thread scratch
buffers on first use. EKIP runs four model-loading processes side by side on
one host in development (`scripts/run_api_server.py`,
`scripts/run_ingestion_worker.py`, the `arq` agents worker, and
`scripts/run_mcp_server.py`), each loading the embedding model and -- for the
agent graph -- the cross-encoder reranker. With the default thread count that
is 4 x 12 pools' worth of buffers competing for the same RAM + page-file
commit budget; observed in practice as
``OpenBLAS error: Memory allocation still failed after 10 retries, giving
up`` followed by a native access violation that kills the whole process
(taking the multi-tenant MCP server down with it).

Inference here is latency-bound on small batches, not throughput-bound on
large ones, so a small fixed pool costs almost nothing and removes the
failure mode. These are the standard, widely-recognized environment
variables; `setdefault` means an operator who has tuned them for a
throughput-heavy single-process deployment still wins.

**Must run before NumPy/OpenBLAS import.** The libraries read these values
once, at their own initialization. `app/__init__.py` calls
`limit_native_math_threads()` at package-init time -- the earliest guaranteed
hook, since importing any `app.*` module runs the `app` package initializer
first, before that module's own (possibly NumPy-importing) body.
"""

from __future__ import annotations

import os

#: Default cap. 2 keeps a little intra-op parallelism for the reranker's
#: forward pass while collapsing the per-thread buffer footprint from
#: "one per core" to two. Override with the real env vars for a deployment
#: that genuinely wants more.
_DEFAULT_THREADS = "2"

_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",       # OpenMP (torch CPU kernels, sentence-transformers)
    "OPENBLAS_NUM_THREADS",  # OpenBLAS (NumPy on most Linux/Windows wheels)
    "MKL_NUM_THREADS",       # Intel MKL (NumPy on MKL builds)
    "NUMEXPR_NUM_THREADS",   # numexpr (pulled in by pandas / transformers)
    "VECLIB_MAXIMUM_THREADS",  # Apple Accelerate (macOS dev machines)
)


def limit_native_math_threads(threads: str = _DEFAULT_THREADS) -> None:
    """Set every native-math thread-count env var that is not already set.

    Idempotent: `os.environ.setdefault` never overrides an operator-provided
    value, and re-calling it is a no-op.
    """
    for name in _THREAD_ENV_VARS:
        os.environ.setdefault(name, threads)
