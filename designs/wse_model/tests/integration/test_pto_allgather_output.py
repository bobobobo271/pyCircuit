"""Opt-in full payload oracle for a separately executed generated simulation.

Set WSE_PTO_ALLGATHER_INPUT and WSE_PTO_ALLGATHER_OUTPUT to its binary files.
Input is 32 node-major blocks of 1536 bytes; output is 32 blocks of 49152 bytes.
This test checks observed bytes; it does not compile or execute the DUT.
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_generated_output_contains_every_original_source_on_every_member():
    input_path = os.environ.get("WSE_PTO_ALLGATHER_INPUT")
    output_path = os.environ.get("WSE_PTO_ALLGATHER_OUTPUT")
    if input_path is None and output_path is None:
        pytest.skip("set input/output paths from a real generated simulation")
    assert input_path and output_path, "both input and output paths are required"
    original = Path(input_path).read_bytes()
    actual = Path(output_path).read_bytes()
    assert len(original) == 32 * 1536
    assert len(actual) == 32 * 49152
    # Row ranks ascend by column; column ranks ascend by row. Their composition
    # is node 0..31 order, preserving all original FP32 bit patterns exactly.
    for destination in range(32):
        observed = actual[destination * 49152 : (destination + 1) * 49152]
        if observed != original:
            offset = next(
                i for i, (a, b) in enumerate(zip(observed, original, strict=True)) if a != b
            )
            source, local_offset = divmod(offset, 1536)
            pytest.fail(
                f"destination {destination}, source {source}, byte {local_offset}: "
                f"got {observed[offset]:02x}, expected {original[offset]:02x}"
            )
