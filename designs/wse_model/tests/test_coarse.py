"""Generated-backend checks with hand-derived NoC timing and resource controls.

Set WSE_COARSE_SIMULATORS to a JSON object mapping backend names to runner paths.
Both runners receive identical loader.bin inputs. WSE_COARSE_TEST_OUTPUT retains
loader descriptions, complete traces, receipts and stdout/stderr per case/backend.
No configured runner means explicit SKIP, never a simulated or inferred PASS.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from verification.coarse_cases import (
    FlowExpectation,
    TraceExpectation,
    calendar_barrier_expectation,
    check_calendar_barrier,
    check_trace,
    open_pipeline_expectations,
    source_queue_expectation,
)
from wse_model.programs.coarse import (
    Barrier,
    Profile,
    RetireGroup,
    Route,
    Transfer,
    WaitGroup,
    Workload,
)

pytestmark = pytest.mark.integration
RUNNERS = json.loads(os.environ.get("WSE_COARSE_SIMULATORS", "{}"))
BACKENDS = list(RUNNERS) or [
    pytest.param(None, marks=pytest.mark.skip(reason="no generated coarse runner configured"))
]


def normalize(receipt, events):
    kinds = {3: "link", 9: "landing", 5: "complete", 10: "barrier"}
    normalized = []
    for event in events:
        if event["kind"] not in kinds:
            continue
        item = {
            "kind": kinds[event["kind"]],
            "cycle": event["noc_cycle"],
            "transfer_id": event["id"],
            "node": event["node"],
            "flit": event["flit"],
            "nbytes": event["bytes"],
        }
        if item["kind"] == "link":
            item["target"] = item["node"] + (-8, -1, 1, 8)[event["direction"]]
        normalized.append(item)
    return {"fault": receipt["fault"], "model_time": receipt["model_time"], "events": normalized}


@pytest.fixture(params=BACKENDS)
def run_backend(request, tmp_path):
    backend = request.param

    def run(name, workload, *, expect_fault=False, extra_env=None):
        base = Path(os.environ.get("WSE_COARSE_TEST_OUTPUT", tmp_path))
        output = base / name / backend
        output.mkdir(parents=True, exist_ok=False)
        loader = workload.write(output)
        receipt_path, trace_path = output / "receipt.json", output / "events.jsonl"
        command = [
            str(Path(RUNNERS[backend]).resolve()),
            str(loader),
            str(receipt_path),
            str(trace_path),
        ]
        with (output / "process.log").open("w") as log:
            execution = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
                env=dict(os.environ, **(extra_env or {})),
            )
        (output / "command.json").write_text(
            json.dumps(
                {
                    "argv": command,
                    "returncode": execution.returncode,
                    "environment_overrides": extra_env or {},
                },
                indent=2,
            )
            + "\n"
        )
        assert execution.returncode == (2 if expect_fault else 0), output / "process.log"
        receipt = json.loads(receipt_path.read_text())
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        assert events or expect_fault, "a completed workload must publish actual model events"
        assert all(
            b["noc_cycle"] >= a["noc_cycle"] for a, b in zip(events, events[1:], strict=False)
        )
        result = normalize(receipt, events)
        result["receipt"] = receipt
        result["loader_sha256"] = hashlib.sha256(loader.read_bytes()).hexdigest()
        return result

    return run


def workload_from(expected, *, profile=None):
    routes = tuple(
        Route(flow.transfer_id, flow.source, flow.edges, flow.targets) for flow in expected.flows
    )
    commands = tuple(
        Transfer(flow.transfer_id, flow.source, flow.transfer_id, flow.nbytes)
        for flow in expected.flows
    )
    return Workload(routes, commands, profile=profile or Profile.open())


@pytest.mark.parametrize("name", list(open_pipeline_expectations()))
def test_open_pipeline_resources(run_backend, name):
    expected = open_pipeline_expectations()[name]
    result = run_backend(name, workload_from(expected))
    check_trace(expected, result)
    receipt = result["receipt"]
    assert receipt["read_bytes"] == sum(flow.nbytes for flow in expected.flows)
    assert receipt["write_bytes"] == sum(flow.nbytes * len(flow.targets) for flow in expected.flows)
    assert receipt["link_bytes"] == sum(
        ((flow.nbytes + 51) // 52) * 64 * len(flow.edges) for flow in expected.flows
    )
    if name == "shared-three-hop":
        # Eight flits share 1→2 from t=1. Final target arrival needs two more
        # ticks. The final tail is at t=10; arbitration may change which
        # transfer finishes first, without changing the shared-link load.
        tails = sorted(event["cycle"] for event in result["events"] if event["kind"] == "complete")
        assert tails[-1] == 10


def test_calendar_origin_after_drained_barrier(run_backend):
    expected = calendar_barrier_expectation()
    workload = workload_from(expected, profile=Profile.open(slot_ticks=2, barrier_ticks=5))
    workload.profile.calendar = {(0, 2, slot): 1 if slot == 1 else 0 for slot in range(8)}
    first, second = workload.commands
    workload.commands = (first, Barrier(), second)
    check_calendar_barrier(run_backend(expected.name, workload))


def test_source_queue_backpressure_resumes_without_loss(run_backend):
    expected = source_queue_expectation()
    check_trace(
        expected,
        run_backend(expected.name, workload_from(expected, profile=Profile.open(capacity=624))),
    )


def test_freed_queue_slot_resumes_later_independent_source(run_backend):
    # Eight descriptors fill the waiting queue. Dispatching the first frees a
    # slot at t=0; the ninth packet and independent source must then issue.
    # Both source ports can start at t=0; queue maintenance adds no NoC ticks.
    flows = tuple(
        FlowExpectation(index, 0, 52, ((0, 1),), (1,), ((1, index),)) for index in range(1, 10)
    ) + (
        FlowExpectation(10, 8, 52, ((8, 9),), (9,), ((9, 1),)),
    )
    expected = TraceExpectation("queue-resume-independent", flows, final_cycle=9)
    check_trace(expected, run_backend(expected.name, workload_from(expected)))


def test_wait_dependency_and_release_with_independent_progress(run_backend):
    flows = (
        FlowExpectation(1, 0, 200, ((0, 1),), (1,), ((1, 4),)),
        FlowExpectation(2, 8, 52, ((8, 9),), (9,), ((9, 5),)),
        FlowExpectation(3, 16, 52, ((16, 17),), (17,), ((17, 1),)),
    )
    expected = TraceExpectation("dependency-and-release", flows, final_cycle=5)
    workload = workload_from(expected)
    workload.commands = (
        Transfer(1, 0, 1, 200, completion_group=0),
        Transfer(2, 8, 2, 52, release_time=2, dependency_group=0, completion_group=1),
        Transfer(3, 16, 3, 52, completion_group=2),
        WaitGroup(1),
    )
    check_trace(expected, run_backend(expected.name, workload))


def test_retirement_releases_receive_capacity(run_backend):
    expected = TraceExpectation(
        "retirement-capacity",
        (
            FlowExpectation(1, 0, 52, ((0, 1),), (1,), ((1, 1),)),
            FlowExpectation(2, 0, 52, ((0, 1),), (1,), ((1, 2),)),
        ),
        final_cycle=2,
    )
    workload = workload_from(expected, profile=Profile.open(capacity=52))
    workload.commands = (
        Transfer(1, 0, 1, 52, completion_group=0),
        WaitGroup(0),
        RetireGroup(0),
        Transfer(2, 0, 2, 52, completion_group=1),
    )
    check_trace(expected, run_backend(expected.name, workload))


def test_over_capacity_packet_has_no_completion(run_backend):
    expected = TraceExpectation(
        "over-capacity", (FlowExpectation(1, 0, 53, ((0, 1),), (1,)),), expected_fault=True
    )
    workload = workload_from(expected, profile=Profile.open(capacity=52))
    result = run_backend(expected.name, workload, expect_fault=True)
    check_trace(expected, result)
    assert result["receipt"]["write_bytes"] == 0


@pytest.mark.parametrize("hop_ticks,complete,finish", ((1, 412, 413), (3, 414, 415)))
def test_endpoint_first_beat_and_hop_latency(run_backend, hop_ticks, complete, finish):
    # Default endpoint service for 200 B is 200 + ceil(10/7) = 202 ticks.
    # Injection begins at 204; four flits occupy 204..207. Final arrival is
    # 207+H, followed by RX=2, write=202 and completion guard=1. Only H varies.
    expected = TraceExpectation(
        f"endpoint-hop-{hop_ticks}",
        (FlowExpectation(1, 0, 200, ((0, 1),), (1,), ((1, complete),)),),
        final_cycle=finish,
    )
    result = run_backend(
        expected.name, workload_from(expected, profile=Profile(hop_ticks=hop_ticks))
    )
    check_trace(expected, result)
    assert tuple(event["cycle"] for event in result["events"] if event["kind"] == "link") == (
        204,
        205,
        206,
        207,
    )


def test_landing_stall_delays_dependency_only(run_backend):
    # Deny only node 1 during [1,7). Four flits then land at 7,8,9,10.
    # A dependent one-hop packet finishes at 11; an unrelated one still ends
    # at 1. All link service and endpoint parameters equal the open controls.
    expected = TraceExpectation(
        "landing-stall-and-dependency",
        (
            FlowExpectation(1, 0, 200, ((0, 1),), (1,), ((1, 10),)),
            FlowExpectation(2, 8, 52, ((8, 9),), (9,), ((9, 1),)),
            FlowExpectation(3, 16, 52, ((16, 17),), (17,), ((17, 11),)),
        ),
        final_cycle=11,
    )
    workload = workload_from(expected)
    workload.commands = (
        Transfer(1, 0, 1, 200, completion_group=0),
        Transfer(2, 8, 2, 52, completion_group=1),
        Transfer(3, 16, 3, 52, dependency_group=0, completion_group=2),
    )
    control = TraceExpectation(
        "landing-stall-control",
        (
            FlowExpectation(1, 0, 200, ((0, 1),), (1,), ((1, 4),)),
            FlowExpectation(2, 8, 52, ((8, 9),), (9,), ((9, 1),)),
            FlowExpectation(3, 16, 52, ((16, 17),), (17,), ((17, 5),)),
        ),
        final_cycle=5,
    )
    baseline = run_backend(control.name, workload)
    check_trace(control, baseline)
    result = run_backend(
        expected.name,
        workload,
        extra_env={"WSE_STALL_NODE": "1", "WSE_STALL_AT": "1", "WSE_STALL_UNTIL": "7"},
    )
    check_trace(expected, result)
    assert baseline["loader_sha256"] == result["loader_sha256"]
    assert tuple(
        event["cycle"]
        for event in result["events"]
        if event["kind"] == "landing" and event["node"] == 1
    ) == (7, 8, 9, 10)
    assert tuple(
        event["cycle"]
        for event in result["events"]
        if event["kind"] == "link" and event["transfer_id"] == 1
    ) == (0, 1, 2, 3)
