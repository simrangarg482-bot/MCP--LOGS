"""EKIP application package.

The one thing that must happen before anything else in every EKIP process:
bound the native math thread pools (OpenBLAS/MKL/OpenMP) that NumPy and
PyTorch pull in transitively. Those libraries read their thread-count
environment variables exactly once, at their own first import, so this has
to run before the first ``import numpy`` anywhere -- and importing any
``app.*`` module runs this package initializer first, which is the earliest
guaranteed hook. See ``app.shared.config.native_runtime`` for the full
rationale (co-located model-loading processes exhausting the host's memory
commit budget).
"""

from app.shared.config.native_runtime import limit_native_math_threads

limit_native_math_threads()
