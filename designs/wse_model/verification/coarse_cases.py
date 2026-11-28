"""Independent expectations for coarse network execution.

The smoke controls use all 40 nodes, 52 useful bytes per flit, 64 wire bytes,
unit directed-link service and hop latency, open Calendar, and zero endpoint
and fixed overhead. Expectations are derived from paths, not DUT timestamps.
The backend adapter supplies accepted events only in the small schema below.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FlowExpectation:
    transfer_id: int
    source: int
    nbytes: int
    edges: tuple[tuple[int, int], ...]
    targets: tuple[int, ...]
    completion_cycles: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class TraceExpectation:
    """A case's literal topology and independently derived completion times."""

    name: str
    flows: tuple[FlowExpectation, ...]
    final_cycle: int | None = None
    barrier_cycles: tuple[int, ...] = ()
    expected_fault: bool = False


def check_trace(expected: TraceExpectation, result: dict[str, Any]) -> None:
    """Check normalized actual events; never use these expectations to drive DUT.

    result has fault, model_time, and events. Every event has kind, cycle and
    transfer_id (except barriers). Accepted link events additionally have node,
    target and flit; landing events have node and flit; complete events have
    node and nbytes. A self-copy has a complete event and no link/landing event.
    Retries are not accepted link or landing events.
    """
    assert bool(result["fault"]) == expected.expected_fault, expected.name
    events = result["events"]
    assert all(b["cycle"] >= a["cycle"] for a, b in zip(events, events[1:], strict=False))
    complete = [event for event in events if event["kind"] == "complete"]
    if expected.expected_fault:
        assert not complete, "a rejected transfer must publish no logical completion"
        return

    flows = {flow.transfer_id: flow for flow in expected.flows}
    assert len(flows) == len(expected.flows), "test transfer identities must be unique"
    assert Counter((event["transfer_id"], event["node"]) for event in complete) == Counter(
        (flow.transfer_id, node) for flow in expected.flows for node in flow.targets
    ), "each intended target completes exactly once"
    for event in complete:
        flow = flows[event["transfer_id"]]
        assert event["nbytes"] == flow.nbytes, "completion counts useful payload bytes"
        cycles = dict(flow.completion_cycles)
        if event["node"] in cycles:
            assert event["cycle"] == cycles[event["node"]], (expected.name, event, cycles)

    links = [event for event in events if event["kind"] == "link"]
    assert Counter(
        (event["transfer_id"], event["node"], event["target"], event["flit"]) for event in links
    ) == Counter(
        (flow.transfer_id, node, target, flit)
        for flow in expected.flows
        for node, target in flow.edges
        for flit in range((flow.nbytes + 51) // 52)
    ), "every tree edge carries each flit once, including the shared multicast trunk"
    link_slots = [(event["node"], event["target"], event["cycle"]) for event in links]
    assert len(link_slots) == len(set(link_slots)), "one accepted flit per directed link/tick"

    landings = [event for event in events if event["kind"] == "landing"]
    assert Counter(
        (event["transfer_id"], event["node"], event["flit"]) for event in landings
    ) == Counter(
        (flow.transfer_id, node, flit)
        for flow in expected.flows
        for node in flow.targets
        if node != flow.source
        for flit in range((flow.nbytes + 51) // 52)
    ), "all remote target flits must be accepted exactly once"
    landing_slots = [(event["node"], event["cycle"]) for event in landings]
    assert len(landing_slots) == len(set(landing_slots)), "one ejection across all ingress links"

    assert tuple(event["cycle"] for event in events if event["kind"] == "barrier") == (
        expected.barrier_cycles
    )
    if expected.final_cycle is not None:
        assert result["model_time"] == expected.final_cycle


def open_pipeline_expectations() -> dict[str, TraceExpectation]:
    """200 B = four flits; an H-hop tail arrives at H + 4 - 1."""
    return {
        "single-hop": TraceExpectation(
            "single-hop",
            (FlowExpectation(1, 0, 200, ((0, 1),), (1,), ((1, 4),)),),
            final_cycle=4,
        ),
        "two-hop": TraceExpectation(
            "two-hop",
            (FlowExpectation(1, 0, 200, ((0, 1), (1, 2)), (2,), ((2, 5),)),),
            final_cycle=5,
        ),
        "multicast-self-node33": TraceExpectation(
            "multicast-self-node33",
            (
                FlowExpectation(
                    1,
                    0,
                    200,
                    ((0, 1), (1, 2), (1, 9), (9, 17), (17, 25), (25, 33)),
                    (0, 2, 33),
                    ((0, 0), (2, 5), (33, 8)),
                ),
            ),
            final_cycle=8,
        ),
        "disjoint-three-hop": TraceExpectation(
            "disjoint-three-hop",
            (
                FlowExpectation(1, 0, 200, ((0, 1), (1, 2), (2, 3)), (3,), ((3, 6),)),
                FlowExpectation(2, 8, 200, ((8, 9), (9, 10), (10, 11)), (11,), ((11, 6),)),
            ),
            final_cycle=6,
        ),
        "shared-three-hop": TraceExpectation(
            "shared-three-hop",
            (
                FlowExpectation(1, 0, 200, ((0, 1), (1, 2), (2, 3)), (3,)),
                FlowExpectation(2, 9, 200, ((9, 1), (1, 2), (2, 10)), (10,)),
            ),
        ),
        "ejection-two-ingresses": TraceExpectation(
            "ejection-two-ingresses",
            (
                FlowExpectation(1, 0, 200, ((0, 1),), (1,)),
                FlowExpectation(2, 9, 200, ((9, 1),), (1,)),
            ),
            final_cycle=8,
        ),
    }


def calendar_barrier_expectation() -> TraceExpectation:
    """Only slot 1 of eight two-tick slots opens link 0→1.

    Four services are at 2,3,18,19 and complete at 20. A drained barrier
    adds alignment=3 and arm=2, establishing origin=25. The next packet's
    services are 27,28,43,44, completing at 45 with the new origin.
    """
    return TraceExpectation(
        "calendar-barrier",
        (
            FlowExpectation(1, 0, 200, ((0, 1),), (1,), ((1, 20),)),
            FlowExpectation(2, 0, 200, ((0, 1),), (1,), ((1, 45),)),
        ),
        final_cycle=45,
        barrier_cycles=(25,),
    )


def check_calendar_barrier(result: dict[str, Any]) -> None:
    check_trace(calendar_barrier_expectation(), result)
    for transfer_id, cycles in ((1, (2, 3, 18, 19)), (2, (27, 28, 43, 44))):
        assert (
            tuple(
                event["cycle"]
                for event in result["events"]
                if event["kind"] == "link" and event["transfer_id"] == transfer_id
            )
            == cycles
        ), "no link service occurs in a closed Calendar slot"


def source_queue_expectation() -> TraceExpectation:
    """12 one-flit packets exceed one active plus eight waiting descriptors.

    The receiver group has exactly 12*52=624 bytes capacity. Each packet needs
    one hop/tick, and the single active-packet slot holds until completion.
    The source queue must pause issue then resume, retaining all identities.
    """
    return TraceExpectation(
        "source-queue-backpressure",
        tuple(
            FlowExpectation(index, 0, 52, ((0, 1),), (1,), ((1, index),)) for index in range(1, 13)
        ),
        final_cycle=12,
    )
