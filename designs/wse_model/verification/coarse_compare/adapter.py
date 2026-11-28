"""Translate explicitly synchronized legacy phases to address-free metadata.

This comparison adapter accepts disjoint source/destination phase storage and
complete WAIT/RETIRE blocks. Addresses are used only for these host checks; the
generated model receives routes, bytes, logical identities and dependencies.
"""

from __future__ import annotations

from collections import deque
from fractions import Fraction

from wse_model.calendar.route_bits import RouteBits
from wse_model.calendar.validate import validate_route_bits
from wse_model.programs.abi import Kind, Program
from wse_model.programs.coarse import (
    Barrier,
    Profile,
    RetireGroup,
    Route,
    Transfer,
    WaitGroup,
    Workload,
)
from wse_model.topology import CALENDAR_BASELINE


def profile_from_legacy(profile: dict, *, calendar: dict[int, int] | None = None) -> Profile:
    """Retain endpoint timing and fixed Calendar unless explicitly overridden."""
    from wse_model.programs.timing import fixed_calendar

    values = {name: row["value"] for name, row in profile["parameters"].items()}
    if values["ub_read_bytes_per_core_cycle"] != values["ub_write_bytes_per_core_cycle"]:
        raise ValueError("coarse profile currently requires equal read and write widths")
    ratio = Fraction(values["noc_clock_hz"], values["core_clock_hz"])
    masks = fixed_calendar()
    if calendar is not None:
        masks.update(calendar)
    return Profile(
        read_first=(values["ub_read_first_beat_ns"] * values["noc_clock_hz"] + 999999999)
        // 1000000000,
        write_first=(values["ub_write_first_beat_ns"] * values["noc_clock_hz"] + 999999999)
        // 1000000000,
        numerator=ratio.numerator,
        denominator=ratio.denominator,
        width=values["ub_read_bytes_per_core_cycle"],
        flit_bytes=values["payload_bytes_per_flit"],
        hop_ticks=values["hop_noc_cycles"],
        slot_ticks=values["slot_noc_cycles"],
        tx_ticks=values["tx_cdc_noc_cycles"],
        rx_ticks=values["rx_cdc_noc_cycles"],
        barrier_ticks=values["alignment_noc_cycles"] + values["arm_noc_cycles"],
        guard_ticks=values["guard_noc_cycles"],
        calendar={
            (address // 32, address // 8 % 4, address % 8): mask for address, mask in masks.items()
        },
        name=(
            "legacy-compatible-calendar"
            if calendar is None
            else "legacy-compatible-calendar-override"
        ),
    )


def _route(command, route_id: int) -> Route:
    topology = CALENDAR_BASELINE.topology
    bits = RouteBits.from_int(command.route, topology.node_count)
    validate_route_bits(
        topology,
        bits,
        source=command.source,
        expected_land_nodes=bits.land_nodes,
        self_delivery=command.source in bits.land_nodes,
    ).raise_if_failed()
    pending = deque([command.source])
    visited = {command.source}
    edges = []
    while pending:
        node = pending.popleft()
        for peer in topology.neighbours(node):
            if peer in bits.pass_nodes and peer not in visited:
                visited.add(peer)
                edges.append((node, peer))
                pending.append(peer)
    targets = set(bits.land_nodes)
    if command.self_copy:
        targets.add(command.source)
    return Route(route_id, command.source, tuple(edges), tuple(sorted(targets)))


def from_legacy(program: Program, profile: Profile) -> Workload:
    """Keep packet order and explicit phase boundaries; never compute timing."""
    routes, commands, packets = [], [], []
    posts, waited, retired = {}, set(), set()
    group = 0
    ended = False

    def check_phase() -> None:
        if not packets or waited != set(posts) or retired != set(posts):
            raise ValueError("comparison requires complete POST/SEND/WAIT/RETIRE phases")
        remote = dict.fromkeys(posts, 0)
        writes = []
        for packet, route in packets:
            for target in route.targets:
                if target != packet.source:
                    if target not in posts:
                        raise ValueError("remote target has no phase receive group")
                    remote[target] += packet.nbytes
                writes.append((target, packet.dst, packet.dst + packet.nbytes))
        for node, post in posts.items():
            if remote[node] != post.expected or post.expected != post.compiled_expected:
                raise ValueError("phase receive bytes disagree with declared expectations")
            if post.capacity > profile.capacity:
                raise ValueError("legacy receive capacity exceeds coarse endpoint capacity")
        for packet, _ in packets:
            if any(
                node == packet.source
                and max(begin, packet.src) < min(end, packet.src + packet.nbytes)
                for node, begin, end in writes
            ):
                raise ValueError(
                    "phase reads alias concurrent writes; explicit logical dependencies required"
                )

    for command in program.commands:
        if ended:
            raise ValueError("commands follow END")
        if command.kind in (Kind.INSTALL, Kind.NEXT_EPOCH):
            continue
        if command.kind == Kind.POST:
            if packets:
                check_phase()
                group += 1
                posts, waited, retired, packets = {}, set(), set(), []
            if command.node in posts:
                raise ValueError("multiple POST records for one node in a phase")
            posts[command.node] = command
        elif command.kind == Kind.BARRIER:
            commands.append(Barrier())
        elif command.kind in (Kind.SEND, Kind.COPY):
            if waited or retired:
                raise ValueError("phase sends must precede waits and retirement")
            route = _route(command, len(routes))
            routes.append(route)
            packets.append((command, route))
            commands.append(
                Transfer(
                    id=len(routes) - 1,
                    source_node=command.source,
                    route_id=route.id,
                    nbytes=command.nbytes,
                    logical_block_id=len(routes) - 1,
                    dependency_group=group - 1 if group else None,
                    completion_group=group,
                )
            )
        elif command.kind == Kind.WAIT:
            if not waited:
                commands.append(WaitGroup(group))
            waited.add(command.node)
        elif command.kind == Kind.RETIRE:
            if not retired:
                commands.append(RetireGroup(group))
            retired.add(command.node)
        elif command.kind == Kind.END:
            check_phase()
            ended = True
        else:
            raise ValueError(f"unsupported comparison instruction {command.kind}")
    if not ended:
        raise ValueError("comparison program must end explicitly")
    result = Workload(tuple(routes), tuple(commands), profile)
    result.records()
    return result
