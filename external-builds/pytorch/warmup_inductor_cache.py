# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
Warmup script for torch.compile / inductor kernel cache.

Exercises a representative set of ops (element-wise, reduction, matmul, scatter,
conv, attention) so that the Triton/HIP kernels for these are compiled and cached
before the inductor test shards run. The resulting cache is exported via
torch.compiler.save_cache_artifacts() and written to a file for upload.

Usage:
    python warmup_inductor_cache.py --output cache_artifacts.bin [--device cuda]
"""

import argparse
import sys
import time
from pathlib import Path

import torch


def _warmup_ops(device: str) -> None:
    """Run torch.compile over representative inductor ops."""
    dtype = torch.float32
    N = 256

    # Element-wise and reductions
    @torch.compile
    def elementwise(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        return (x.sin() + y.cos()).relu()

    @torch.compile
    def reduction(x: torch.Tensor) -> torch.Tensor:
        return x.sum(dim=-1) + x.mean(dim=0)

    # Matmul
    @torch.compile
    def matmul(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
        return torch.mm(a, b)

    # Scatter / gather (exercised heavily by opinfo)
    @torch.compile
    def scatter_gather(src: torch.Tensor, idx: torch.Tensor) -> torch.Tensor:
        out = torch.zeros(N, N, device=device, dtype=dtype)
        out.scatter_add_(0, idx, src)
        return out.gather(0, idx)

    # Softmax + layer norm (common inductor patterns)
    @torch.compile
    def norm_ops(x: torch.Tensor) -> torch.Tensor:
        return torch.nn.functional.layer_norm(
            torch.nn.functional.softmax(x, dim=-1), [N]
        )

    # Conv2d (default shard exercises this)
    @torch.compile
    def conv(x: torch.Tensor, w: torch.Tensor) -> torch.Tensor:
        return torch.nn.functional.conv2d(x, w, padding=1)

    shapes = [
        (N, N),
        (N * 2, N * 2),
        (N // 2, N // 2),
    ]
    print(f"Warming up inductor cache on device={device} ...")
    t0 = time.time()

    for H, W in shapes:
        x = torch.rand(H, W, device=device, dtype=dtype)
        y = torch.rand(H, W, device=device, dtype=dtype)
        elementwise(x, y)
        reduction(x)

        a = torch.rand(H, W, device=device, dtype=dtype)
        b = torch.rand(W, H, device=device, dtype=dtype)
        matmul(a, b)

        src = torch.rand(H, W, device=device, dtype=dtype)
        idx = torch.randint(0, H, (H, W), device=device)
        scatter_gather(src, idx)

        norm_ops(x)

    # Conv uses 4D tensors
    for C in [4, 8, 16]:
        x4 = torch.rand(2, C, N, N, device=device, dtype=dtype)
        w4 = torch.rand(C, C, 3, 3, device=device, dtype=dtype)
        conv(x4, w4)

    elapsed = time.time() - t0
    print(f"Warmup completed in {elapsed:.1f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm up inductor kernel cache")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write serialized cache artifacts",
    )
    parser.add_argument(
        "--device",
        default="cuda",
        help="Device to compile for (default: cuda, which maps to HIP on ROCm)",
    )
    args = parser.parse_args()

    if not torch.cuda.is_available():
        print("ERROR: No GPU available; cannot warm up inductor cache.", file=sys.stderr)
        return 1

    _warmup_ops(args.device)

    result = torch.compiler.save_cache_artifacts()
    if result is None:
        print("WARNING: save_cache_artifacts() returned None — no cache produced.")
        args.output.write_bytes(b"")
    else:
        artifact_bytes, cache_info = result
        args.output.write_bytes(artifact_bytes)
        print(f"Cache info: {cache_info}")
        print(f"Saved {len(artifact_bytes):,} bytes → {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
