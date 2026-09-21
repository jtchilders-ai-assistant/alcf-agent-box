#!/usr/bin/env python3
"""Prove a rank can query its assigned GPU without requiring PyTorch."""

import os
import subprocess


device = os.environ["CUDA_VISIBLE_DEVICES"]
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
