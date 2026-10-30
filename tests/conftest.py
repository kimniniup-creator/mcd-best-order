"""Tests written before r32 exercise the human-confirmation gate, which is OFF by default since r32 (internal-tool mode).
They run with the switch on; tests/test_r32.py flips it off per test (the switch is read at call time)."""
import os

os.environ['COUPON_SPLIT_HUMAN_CONFIRM'] = 'true'
