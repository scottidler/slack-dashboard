"""Calibration arena (Karpathy Autoresearch): deterministic boards, binary criteria,
a scoring harness, and a keep/discard tuning loop.

The arena is a dev tool in Phase 4 (run via ``bin/calibrate.py``); Phase 5 freezes the
board + criteria as ``tests/calibration/test_calibration.py`` under ``otto ci``. Every
module here is pure Python, deterministic, and network-free so it runs in milliseconds.
"""
