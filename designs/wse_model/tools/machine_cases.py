"""Small, independently specified programs for one compiled WSE machine.

The byte strings and routes below are test-owned constants. No result is
computed by the hardware model or the AllGather program generator. All cases
use the same 40-node, 64 KiB-per-node hardware profile and an 8-byte beat.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from wse_model.programs.abi import Command, Kind, MemoryPatch, MemoryRegion, Program

SENTINEL = b"\xcc" * 8
GROUP_BYTES = {
    9: bytes.fromhex("909192939495969798999a9b9c9d9e9f"),
    0: bytes.fromhex("000102030405060708090a0b0c0d0e0f"),
    17: bytes.fromhex("a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"),
}
GROUP_OUTPUT = bytes.fromhex(
    "909192939495969798999a9b9c9d9e9f"
    "000102030405060708090a0b0c0d0e0f"
    "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"
)
TREE_BYTES = bytes.fromhex("102132435465768798a9bacbdcedfe0f2031425364758697")
TREE_ALTERNATE = bytes.fromhex("a0b1c2d3e4f5061728394a5b6c7d8e9f90a1b2c3d4e5f607")
# Tree 9 -> {1, 10, 17}; 17 -> 25 -> 33. Only 1, 10, 33 land.
TREE_ROUTE = (3 << 2) | (2 << 18) | (3 << 20) | (2 << 34) | (2 << 50) | (3 << 66)


@dataclass(frozen=True)
class MachineCase:
    name: str
    program: Program
    patches: tuple[MemoryPatch, ...]
    readback: tuple[MemoryRegion, ...]
    expected_bytes: bytes
    expected_counts: dict[int, int]
    stall_copy_pc: int | None = None
    expect_fault: bool = False


def _setup(nodes: tuple[int, ...]) -> list[Command]:
    return [Command(Kind.INSTALL, node=node, opcode=1) for node in nodes] + [
        Command(Kind.NEXT_EPOCH, node=node, opcode=1) for node in nodes
    ]


def _post(node: int, dst: int, capacity: int, expected: int) -> Command:
    return Command(
        Kind.POST,
        node=node,
        opcode=1,
        epoch=1,
        dst=dst,
        capacity=capacity,
        expected=expected,
        compiled_expected=expected,
    )


def _finish(nodes: tuple[int, ...]) -> list[Command]:
    return (
        [Command(Kind.WAIT, node=node, opcode=1, epoch=1) for node in nodes]
        + [Command(Kind.RETIRE, node=node) for node in nodes]
        + [Command(Kind.END)]
    )


def changed_group_allgather() -> MachineCase:
    """Three nonconsecutive ranks, two fragments each, remote count 2 * 16."""
    members = (9, 0, 17)
    # Induced tree 0--1--9--17. Node 1 passes without landing.
    routes = {9: 0xC0008000B, 0: 0xC000C000A, 17: 0x8000C000B}
    commands = _setup(members)
    commands += [_post(node, 1536, 48, 32) for node in members]
    first_copy = len(commands)
    for rank, source in enumerate(members):
        for fragment in range(2):
            commands.append(
                Command(
                    Kind.COPY,
                    opcode=1,
                    epoch=1,
                    seq=fragment,
                    source=source,
                    src=256 + fragment * 8,
                    dst=1536 + rank * 16 + fragment * 8,
                    nbytes=8,
                    route=routes[source],
                    self_copy=True,
                )
            )
    commands += _finish(members)
    observed = (*members, 1)
    patches = tuple(MemoryPatch(node, 256, GROUP_BYTES[node]) for node in members) + tuple(
        MemoryPatch(node, 1528, SENTINEL * 8) for node in observed
    )
    return MachineCase(
        name="changed-group-allgather",
        program=Program(tuple(commands)),
        patches=patches,
        readback=tuple(MemoryRegion(node, 1528, 64) for node in observed),
        expected_bytes=(SENTINEL + GROUP_OUTPUT + SENTINEL) * 3 + SENTINEL * 8,
        expected_counts={9: 32, 0: 32, 17: 32, 1: 0},
        stall_copy_pc=first_copy,
    )


def tree_copy_duplicate() -> MachineCase:
    """24-byte broadcast, local source replacement, then a changed duplicate.

    Withhold a landing permit at the first COPY's offset 8 for three write
    attempts to exercise a pause after its first successful 8-byte beat. Counts
    become 24 only when the last beat commits. The duplicate reads changed
    source bytes but must leave all original destination bytes intact.
    """
    targets = (1, 10, 33)
    commands = _setup((9, *targets))
    commands += [_post(node, 2048, 24, 24) for node in targets]
    first_copy = len(commands)
    broadcast = Command(
        Kind.COPY,
        opcode=1,
        epoch=1,
        seq=0,
        source=9,
        src=256,
        dst=2048,
        nbytes=24,
        route=TREE_ROUTE,
    )
    commands += [
        broadcast,
        Command(
            Kind.COPY,
            opcode=1,
            epoch=1,
            seq=1,
            source=9,
            src=512,
            dst=256,
            nbytes=24,
            route=2 << 18,
            self_copy=True,
        ),
        broadcast,
    ]
    commands += _finish(targets)
    untouched = (9, 17, 25, 39)
    observed = (*targets, *untouched)
    patches = (
        MemoryPatch(9, 256, TREE_BYTES),
        MemoryPatch(9, 512, TREE_ALTERNATE),
    ) + tuple(MemoryPatch(node, 2040, SENTINEL * 5) for node in observed)
    return MachineCase(
        name="tree-copy-duplicate",
        program=Program(tuple(commands)),
        patches=patches,
        readback=tuple(MemoryRegion(node, 2040, 40) for node in observed)
        + (MemoryRegion(9, 256, 24),),
        expected_bytes=(SENTINEL + TREE_BYTES + SENTINEL) * 3 + SENTINEL * 20 + TREE_ALTERNATE,
        expected_counts={1: 24, 10: 24, 33: 24, 9: 0, 17: 0, 25: 0, 39: 0},
        stall_copy_pc=first_copy,
    )


def bad_range() -> MachineCase:
    """Change only first COPY dst by one beat: preflight must reject all writes."""
    good = tree_copy_duplicate()
    commands = list(good.program.commands)
    assert good.stall_copy_pc is not None
    commands[good.stall_copy_pc] = replace(commands[good.stall_copy_pc], dst=2056)
    return replace(
        good,
        name="bad-range",
        program=Program(tuple(commands)),
        expected_bytes=SENTINEL * 35 + TREE_BYTES,
        expected_counts=dict.fromkeys(good.expected_counts, 0),
        stall_copy_pc=None,
        expect_fault=True,
    )


def disconnected_route() -> MachineCase:
    """Keep the broadcast program fixed except an unreachable P/L island at 39."""
    good = tree_copy_duplicate()
    commands = list(good.program.commands)
    assert good.stall_copy_pc is not None
    first = commands[good.stall_copy_pc]
    commands[good.stall_copy_pc] = replace(first, route=first.route | (3 << 78))
    return replace(
        good,
        name="disconnected-route",
        program=Program(tuple(commands)),
        expected_bytes=SENTINEL * 35 + TREE_BYTES,
        expected_counts=dict.fromkeys(good.expected_counts, 0),
        stall_copy_pc=None,
        expect_fault=True,
    )


def cases() -> dict[str, MachineCase]:
    result = (changed_group_allgather(), tree_copy_duplicate(), bad_range(), disconnected_route())
    return {case.name: case for case in result}
