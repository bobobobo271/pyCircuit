"""Address-free two-phase AllGather over 32 participants in a 5x8 mesh."""

from __future__ import annotations

from wse_model.programs.coarse import (
    Barrier,
    Profile,
    RetireGroup,
    Route,
    Transfer,
    WaitGroup,
    Workload,
)


def allgather32(profile: Profile, *, bytes_per_rank: int = 128) -> Workload:
    """Gather eight ranks per row, then four complete rows per column.

    Each phase has one packet per source. Local copies consume endpoint write
    service and capacity through their source's inclusion in the route targets.
    """
    if bytes_per_rank <= 0 or bytes_per_rank % 8:
        raise ValueError("rank payload must contain whole 8-byte source units")
    if 32 * bytes_per_rank > profile.capacity:
        raise ValueError("AllGather output exceeds the endpoint capacity")
    phases = (
        tuple(tuple(row * 8 + column for column in range(8)) for row in range(4)),
        tuple(tuple(row * 8 + column for row in range(4)) for column in range(8)),
    )
    routes, commands = [], []
    for phase, groups in enumerate(phases):
        commands.append(Barrier())
        for source in range(32):
            group = next(group for group in groups if source in group)
            rank = group.index(source)
            edges = tuple(
                (
                    (group[index + 1], group[index])
                    if index < rank
                    else (group[index], group[index + 1])
                )
                for index in range(len(group) - 1)
            )
            route_id = len(routes)
            routes.append(Route(route_id, source, edges, group))
            commands.append(
                Transfer(
                    id=route_id,
                    source_node=source,
                    route_id=route_id,
                    nbytes=bytes_per_rank * (8 if phase else 1),
                    logical_block_id=route_id,
                    dependency_group=0 if phase else None,
                    completion_group=phase,
                )
            )
        commands.extend((WaitGroup(phase), RetireGroup(phase)))
    return Workload(tuple(routes), tuple(commands), profile)
