#!/usr/bin/env python3
"""Prove a rank can query its assigned GPU without requiring PyTorch."""

import os
import subprocess
import sys


device = os.environ.get("CUDA_VISIBLE_DEVICES")
if not device:
    print("CUDA_VISIBLE_DEVICES is not set", file=sys.stderr)
    raise SystemExit(2)
subprocess.run(
    [
        "nvidia-smi",
        "-i",
        device,
        "--query-gpu=name,compute_cap",
        "--format=csv,noheader",
    ],
    check=True,
)
