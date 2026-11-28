"""Shared, deterministic inputs for detailed/coarse generated-model comparisons.

These are ordinary runtime communication programs, without a PTO compiler claim.
The independent oracle concatenates original rank bytes, not simulator output.
"""

from __future__ import annotations

from machine_cases import MachineCase
from timed_machine_cases import two_hop_pipeline

from wse_model.programs.abi import MemoryPatch, MemoryRegion, Program
from wse_model.programs.allgather import allgather_program
from wse_model.programs.timing import timed_program


def allgather32(*, timed: bool = True) -> MachineCase:
    rows = tuple(tuple(row * 8 + column for column in range(8)) for row in range(4))
    columns = tuple(tuple(row * 8 + column for row in range(4)) for column in range(8))
    row_program = allgather_program(
        rows,
        source_address=0,
        destination_address=8192,
        bytes_per_source=128,
        fragment_bytes=128,
        opcode=1,
    )
    column_program = allgather_program(
        columns,
        source_address=8192,
        destination_address=32768,
        bytes_per_source=1024,
        fragment_bytes=1024,
        opcode=2,
    )
    program = Program(row_program.commands[:-1] + column_program.commands)
    originals = tuple(bytes((node * 7 + index) % 256 for index in range(128)) for node in range(32))
    return MachineCase(
        name="allgather32-128B",
        program=timed_program(program) if timed else program,
        patches=tuple(MemoryPatch(node, 0, data) for node, data in enumerate(originals)),
        readback=tuple(MemoryRegion(node, 32768, 4096) for node in range(32)),
        expected_bytes=b"".join(originals) * 32,
        expected_counts=dict.fromkeys(range(32), 3072),
        expected_traffic=(36864, 163840),
    )


def comparison_cases() -> tuple[MachineCase, ...]:
    return two_hop_pipeline(), allgather32()
