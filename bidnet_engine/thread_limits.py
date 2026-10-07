"""Force BLAS/OpenMP single-thread before heavy imports."""

from __future__ import annotations

import os
from typing import Any

_VARS = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


def apply_thread_limits(*, n: int = 1) -> dict[str, str]:
    applied: dict[str, str] = {}
    for key in _VARS:
        os.environ[key] = str(n)
        applied[key] = str(n)
    return applied


def verify_thread_limits() -> dict[str, Any]:
    values = {k: os.environ.get(k) for k in _VARS}
    active = all(str(values.get(k) or "") == "1" for k in _VARS)
    return {"values": values, "verified_active": active}


# Apply on import so engine modules inherit caps even if shell export was missed.
apply_thread_limits(n=1)
