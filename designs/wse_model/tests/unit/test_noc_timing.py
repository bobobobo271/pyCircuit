"""Cycle-level timing of the passive NoC and the FFN AllGather."""

from __future__ import annotations

import pytest

from wse_model.calendar.route_bits import RouteBits
from wse_model.errors import CalendarError
from wse_model.fixtures import (
    FFN_PHASE_B_ROW_BYTES,
    FFN_PHASE_C_ROW_BYTES,
    FFN_ROW_COUNT,
    ffn_example,
)
from wse_model.noc import Noc
from wse_model.noc.flit import FlitHeader, flit_count
from wse_model.noc.timing import (
    NocTiming,
    ParameterStatus,
    TimedSend,
    flit_byte_profile,
    simulate_phase,
    time_allgather,
    time_two_phase_allgather,
)
from wse_model.topology import MeshTopology

pytestmark = pytest.mark.unit

NODES = 40


# -- flit profile ------------------------------------------------------------


def test_the_ffn_row_profile_sums_to_the_payload() -> None:
    header = NocTiming().header(NODES)
    profile = flit_byte_profile(
        row_count=FFN_ROW_COUNT, row_bytes=FFN_PHASE_B_ROW_BYTES, header=header
    )
    # 192 B is four flits of 52 B payload; the last carries 36 B.
    assert len(profile) == 4 * FFN_ROW_COUNT
    assert profile[:4] == (52, 52, 52, 36)
    assert sum(profile) == FFN_ROW_COUNT * FFN_PHASE_B_ROW_BYTES


def test_the_column_profile_uses_thirty_flits_per_row() -> None:
    header = NocTiming().header(NODES)
    profile = flit_byte_profile(
        row_count=FFN_ROW_COUNT, row_bytes=FFN_PHASE_C_ROW_BYTES, header=header
    )
    assert len(profile) == 30 * FFN_ROW_COUNT
    assert sum(profile) == FFN_ROW_COUNT * FFN_PHASE_C_ROW_BYTES


# -- the two FFN phases ------------------------------------------------------


@pytest.fixture(scope="module")
def two_phase():
    return time_two_phase_allgather(ffn_example())


def test_phase_b_reaches_the_directed_link_floor(two_phase) -> None:
    phase = two_phase.phases[0]
    # Eight sources on a 1x8 row: every direction-1 link carries the seven
    # sources on the far side, 7 x 32 flits = 224. With a one-cycle hop the
    # inclusive span is 225 cycles.
    assert max(link.copies for link in phase.links) == 224
    assert phase.lower_bound_cycles == 225
    assert phase.transfer_cycles == 225
    assert phase.peak_link_utilization == 1.0


def test_phase_c_reaches_the_directed_link_floor(two_phase) -> None:
    phase = two_phase.phases[1]
    # Four sources on a four-node column: 3 x 240 = 720 on the link nearest an
    # endpoint; the deepest path is three hops.
    assert max(link.copies for link in phase.links) == 720
    assert phase.lower_bound_cycles == 721
    assert phase.transfer_cycles == 721
    assert phase.peak_link_utilization == 1.0


def test_every_destination_counts_exactly_its_exp_val(two_phase) -> None:
    two_phase.check()
    for phase in two_phase.phases:
        for node, expected in phase.exp_val.items():
            assert phase.counted_bytes[node] == expected
        assert phase.complete


def test_the_two_phase_total_includes_both_arm_leads(two_phase) -> None:
    # 225 + 64 (phase B) and 721 + 64 (phase C).
    assert two_phase.transfer_cycles == 225 + 721
    assert two_phase.phase_cycles == 1074
    assert two_phase.seconds == pytest.approx(1074 / 2.0e9)
    assert two_phase.lower_bound_cycles == 1074


def test_links_never_report_more_work_than_their_span(two_phase) -> None:
    for phase in two_phase.phases:
        for link in phase.links:
            assert 1 <= link.busy_cycles <= link.span_cycles
            assert link.first_busy_cycle <= link.last_busy_cycle


def test_the_sources_carry_the_ffn_payload_volumes(two_phase) -> None:
    phase_b = two_phase.phases[0]
    phase_c = two_phase.phases[1]
    assert {source.payload_bytes for source in phase_b.sources} == {8 * 192}
    assert {source.flits for source in phase_b.sources} == {32}
    # The deepest tree is the one from an endpoint of the eight-node row.
    assert max(source.max_depth for source in phase_b.sources) == 7
    assert {source.payload_bytes for source in phase_c.sources} == {8 * 1536}
    assert {source.flits for source in phase_c.sources} == {240}
    assert max(source.max_depth for source in phase_c.sources) == 3


# -- release policy ----------------------------------------------------------


def test_offsets_shift_the_whole_phase() -> None:
    example = ffn_example()
    timing = NocTiming(release_policy="offsets")
    offsets = dict.fromkeys(example.phase_b_groups, 10)
    phase = time_allgather(
        Noc(example.table.topology, calreg=example.calreg()),
        example.table,
        0,
        geometry=example.phase_b_geometry,
        groups=example.phase_b_groups,
        timing=timing,
        release_offsets=offsets,
    )
    assert phase.start_cycle == 10
    assert phase.last_landing_cycle == 10 + 224
    assert phase.transfer_cycles == 225


def test_offsets_require_the_policy_flag() -> None:
    example = ffn_example()
    offsets = dict.fromkeys(example.phase_b_groups, 0)
    with pytest.raises(CalendarError, match="release_policy"):
        time_allgather(
            Noc(example.table.topology, calreg=example.calreg()),
            example.table,
            0,
            geometry=example.phase_b_geometry,
            groups=example.phase_b_groups,
            release_offsets=offsets,
        )


def test_the_offsets_policy_requires_every_member() -> None:
    example = ffn_example()
    offsets = dict.fromkeys(sorted(example.phase_b_groups)[:-1], 0)
    with pytest.raises(CalendarError, match="missing"):
        time_allgather(
            Noc(example.table.topology, calreg=example.calreg()),
            example.table,
            0,
            geometry=example.phase_b_geometry,
            groups=example.phase_b_groups,
            timing=NocTiming(release_policy="offsets"),
            release_offsets=offsets,
        )


# -- fail-closed -------------------------------------------------------------


def test_an_uninstalled_opcode_faults_before_timing() -> None:
    example = ffn_example()
    with pytest.raises(CalendarError, match="not installed"):
        time_allgather(
            Noc(example.table.topology),
            example.table,
            0,
            geometry=example.phase_b_geometry,
            groups=example.phase_b_groups,
        )


def test_a_cyclic_bitmap_is_rejected() -> None:
    topology = MeshTopology(rows=2, cols=2, name="mesh-2x2")
    cyclic = RouteBits.from_sets(4, pass_nodes={0, 1, 2, 3}, land_nodes={1, 2, 3})
    with pytest.raises(CalendarError, match="not a tree"):
        simulate_phase(
            topology,
            (
                TimedSend(
                    source=0,
                    route_bits=cyclic,
                    flit_bytes=(52,),
                ),
            ),
        )


def test_a_bitmap_for_another_node_count_is_rejected() -> None:
    route = RouteBits.from_sets(4, pass_nodes={0, 1}, land_nodes={1})
    with pytest.raises(CalendarError, match="covers"):
        simulate_phase(
            MeshTopology(rows=1, cols=2, name="mesh-1x2"),
            (TimedSend(source=0, route_bits=route, flit_bytes=(52,)),),
        )


# -- open item Q1 ------------------------------------------------------------


def test_the_48_node_header_adds_one_flit_per_row() -> None:
    """Q1: at 48 nodes the 14 B header leaves 50 B payload, so 1536 B is 31 flits."""
    topology = MeshTopology(rows=6, cols=8, name="mesh-6x8")
    timing = NocTiming()
    header = timing.header(topology.node_count)
    assert header.header_bytes == 14
    assert header.payload_bytes == 50
    assert flit_count(1536, header) == 31

    row = tuple(range(8))
    sends = tuple(
        TimedSend.from_geometry(
            source=node,
            route_bits=RouteBits.from_sets(
                topology.node_count,
                pass_nodes=row,
                land_nodes=set(row) - {node},
            ),
            row_count=8,
            row_bytes=1536,
            header=header,
        )
        for node in row
    )
    phase = simulate_phase(topology, sends, timing=timing)
    assert {source.flits for source in phase.sources} == {31 * 8}
    # Seven sources on the far side of the link nearest an endpoint.
    assert max(link.copies for link in phase.links) == 7 * 31 * 8
    assert phase.complete


# -- provenance --------------------------------------------------------------


def test_every_timing_parameter_declares_its_status() -> None:
    parameters = {parameter.name: parameter for parameter in NocTiming().parameters()}
    assert parameters["link_width_bytes"].status is ParameterStatus.DOCUMENTED
    assert parameters["clock_hz"].status is ParameterStatus.DOCUMENTED
    assert parameters["epoch_tag_bits"].status is ParameterStatus.BASELINE
    for name in (
        "hop_latency_cycles",
        "injection_flits_per_cycle",
        "release_policy",
        "land_latency_cycles",
    ):
        assert parameters[name].status is ParameterStatus.DECLARED
        assert parameters[name].source


def test_illegal_timing_values_are_rejected() -> None:
    with pytest.raises(Exception, match="one cycle"):
        NocTiming(hop_latency_cycles=0)
    with pytest.raises(Exception, match="injection"):
        NocTiming(injection_flits_per_cycle=0)
    with pytest.raises(Exception, match="release_policy"):
        NocTiming(release_policy="whenever")
    with pytest.raises(Exception, match="link_width"):
        NocTiming(link_width_bytes=0)


def test_the_header_is_the_calendar_baseline() -> None:
    header = NocTiming().header(NODES)
    assert isinstance(header, FlitHeader)
    assert header.header_bytes == 12
    assert header.payload_bytes == 52
