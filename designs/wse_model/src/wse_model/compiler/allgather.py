"""Calendar lowering for the bounded rank-major PTO AllGather profile.

The PTO frontend supplies verified transfers. This module owns WSE layout,
routing and fragmentation, not the semantics of arbitrary PTO instructions.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from wse_model.calendar.geometry import PayloadGeometry, SymmetricArena
from wse_model.fixtures import group_allgather_bitmap
from wse_model.noc.forward import simulate_multicast
from wse_model.topology import CALENDAR_BASELINE


@dataclass(frozen=True)
class Descriptor:
    phase: int
    source: int
    fragment: int
    nbytes: int
    source_address: int
    destination_address: int
    route: int
    expected: int
    arena_base: int
    arena_capacity: int

    def pack(self) -> bytes:
        return struct.pack(
            "<12I",
            self.phase,
            self.source,
            self.fragment,
            self.nbytes,
            self.source_address,
            self.destination_address,
            self.route & 0xFFFFFFFF,
            (self.route >> 32) & 0xFFFFFFFF,
            self.route >> 64,
            self.expected,
            self.arena_base,
            self.arena_capacity,
        )


def lower_calendar(program: dict, transfers: list[list[dict]]) -> list[Descriptor]:
    """Lower proved all-to-all copies into one multicast per source fragment.

    Each transfer set was extracted from the post-PTOAS SSA for every local
    rank. Exact source/destination coverage is checked before multicast fusion.
    """
    if program["schema"] != "wse-pypto-allgather/1":
        raise ValueError("unsupported program schema")
    if program["topology"] != [5, 8] or program["members"] != list(range(32)):
        raise ValueError("this executable profile requires the 40-node/32-member carrier")
    if program["bank_bytes"] != 65536 or len(program["phases"]) != 2:
        raise ValueError("unsupported memory or phase profile")
    phases = program["phases"]
    if phases[1]["source_base"] != phases[0]["target_base"]:
        raise ValueError("column must read the actual row output")
    if phases[1]["words"] != phases[0]["ranks"] * phases[0]["words"]:
        raise ValueError("column flatten view has the wrong size")
    expected_dependency = {
        "producer": "row.target",
        "consumer": "column.source",
        "view": "contiguous-flatten",
        "bytes": phases[0]["ranks"] * phases[0]["words"] * 4,
    }
    if program["dependency"] != expected_dependency:
        raise ValueError("unsupported phase dependency")
    topology = CALENDAR_BASELINE.topology
    descriptors = []
    for phase_id, (phase, copies) in enumerate(zip(phases, transfers, strict=True)):
        ranks, words = phase["ranks"], phase["words"]
        if (ranks, words, phase["fragments"]) != ((8, 384, 8), (4, 3072, 8))[phase_id]:
            raise ValueError("unsupported executable geometry")
        if phase["dtype"] != "f32" or phase["layout"] != "rank-major-contiguous":
            raise ValueError("expected contiguous FP32 rank-major storage")
        if phase["input_shape"] != [1, words] or phase["output_shape"] != [ranks, words]:
            raise ValueError("shape metadata disagrees with the contiguous kernel ABI")
        expected_copies = [
            {
                "source_rank": source,
                "target_rank": target,
                "source_offset": 0,
                "target_offset": source * words * 4,
                "nbytes": words * 4,
            }
            for source in range(ranks)
            for target in range(ranks)
        ]
        if copies != expected_copies:
            raise ValueError("PTOAS transfers do not implement the declared rank-ordered AllGather")
        groups = phase["groups"]
        expected_groups = (
            [list(range(r * 8, r * 8 + 8)) for r in range(4)]
            if phase_id == 0
            else [list(range(c, 32, 8)) for c in range(8)]
        )
        if groups != expected_groups:
            raise ValueError("group ordering must preserve the source rank order")
        logical_bytes = words * 4
        geometry = PayloadGeometry(1, logical_bytes, logical_bytes, 1, ranks * logical_bytes)
        geometry.check_source_alignment()
        arena = SymmetricArena.packed(
            recv_sym_base=phase["target_base"],
            row_count=1,
            row_bytes=logical_bytes,
            member_ranks=tuple(range(ranks)),
        )
        arena.validate_fill()
        src_start, dst_start = phase["source_base"], phase["target_base"]
        if (src_start, dst_start) != ((0, 4096), (4096, 16384))[phase_id]:
            raise ValueError("unsupported executable address profile")
        if not (0 <= src_start < src_start + logical_bytes <= 65536):
            raise ValueError("source range exceeds the modeled bank")
        if not (0 <= dst_start < dst_start + geometry.capacity <= 65536):
            raise ValueError("target range exceeds the modeled bank")
        if max(src_start, dst_start) < min(
            src_start + logical_bytes, dst_start + geometry.capacity
        ):
            raise ValueError("source and target buffers overlap")
        for source in range(32):
            group = next(tuple(group) for group in groups if source in group)
            rank = group.index(source)
            route = group_allgather_bitmap(topology, group, source)
            trace = simulate_multicast(topology, route, source=source)
            if set(trace.land_nodes) != set(group) - {source}:
                raise ValueError("Calendar route does not reach the transfer destinations")
            fragment_bytes = logical_bytes // 8
            for fragment in range(8):
                descriptors.append(
                    Descriptor(
                        phase_id,
                        source,
                        fragment,
                        fragment_bytes,
                        src_start + fragment * fragment_bytes,
                        arena.dst_address(rank, 0) + fragment * fragment_bytes,
                        route.to_int(),
                        geometry.exp_val(ranks - 1),
                        dst_start,
                        geometry.capacity,
                    )
                )
    return descriptors
