"""Cycle-level NoC timing on top of the Calendar forwarding rules.

The semantic layer (:mod:`wse_model.noc.network`) answers "how many bytes cross
which link"; it deliberately does not answer "when". This module adds the time
axis without changing any of those rules:

* one flit is one link-width transfer and occupies one cycle of one **directed**
  link (whitepaper §4.4: 64 B x 2 GHz = 128 GB/s per direction);
* a multicast fork replicates the flit to every egress port in the same cycle,
  so the copies then travel independently;
* a link serves flits in arrival order, one per cycle;
* a flit that arrives at a node is available to that node's egress links after
  ``hop_latency_cycles``.

What the design sources fix, and what they do not
-------------------------------------------------

Fixed by the sources: link width (64 B), router clock (2 GHz), the 12 B per-flit
header at 40 nodes, the passive ``{P, L}`` forwarding rule, and the fact that a
hop has no routing-table lookup. **Not** fixed: the router pipeline depth, the
arbitration policy, the injection rate, and — most importantly — the ``CalReg``
timeslot schedule, which Calendar §2.7.1 makes the NoC algorithm's own output.

This model therefore reports two things separately:

``cycles``
    the simulated makespan under an explicit release policy. The default,
    ``"simultaneous"``, releases every member at cycle 0. That is a contention
    stress case, not the Calendar schedule.
``lower_bound_cycles``
    the schedule-independent floor: the busiest directed link's flit count,
    the largest per-source injection count, and the deepest tree path. A
    conflict-free ``CalReg`` schedule can approach this; proving that schedule
    exists is the NoC algorithm's responsibility (open item ``C-4``).

Every parameter that the sources leave open is carried as a
:class:`TimingParameter` with a status. Nothing here silently invents a design
value; the report prints the provenance next to the number.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from enum import Enum

from wse_model.analysis.platform import NOC_CLOCK_HZ, NOC_LINK_WIDTH_BYTES
from wse_model.calendar.collective import Collective
from wse_model.calendar.geometry import PayloadGeometry
from wse_model.calendar.route_bits import RouteBits
from wse_model.calendar.table import CalendarRouteTable
from wse_model.errors import CalendarError, WseModelError
from wse_model.noc.flit import DEFAULT_EPOCH_TAG_BITS, FlitHeader, flit_count
from wse_model.noc.forward import simulate_multicast
from wse_model.noc.network import Noc
from wse_model.topology import MeshTopology

__all__ = [
    "LinkTiming",
    "NocTiming",
    "ParameterStatus",
    "PhaseTiming",
    "SourceTiming",
    "TimedSend",
    "TimingParameter",
    "TwoPhaseTiming",
    "time_allgather",
    "time_two_phase_allgather",
]


class ParameterStatus(str, Enum):
    """Where a timing parameter's value comes from."""

    DOCUMENTED = "documented"
    """Stated by a design source (whitepaper or Calendar)."""

    BASELINE = "baseline"
    """An adopted reading of an open item, recorded in a decision record."""

    DECLARED = "declared-input"
    """Not fixed by the design sources; supplied by the scenario."""


@dataclass(frozen=True)
class TimingParameter:
    """One timing-model input, with its provenance."""

    name: str
    value: object
    unit: str
    status: ParameterStatus
    source: str

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "status": self.status.value,
            "source": self.source,
        }


# -- parameters --------------------------------------------------------------

#: The router pipeline depth. Calendar says a hop has no routing-table lookup;
#: it does not fix how many cycles the flit spends in the router. One cycle is
#: the minimum consistent with one flit per link per cycle.
DEFAULT_HOP_LATENCY_CYCLES = 1


@dataclass(frozen=True)
class NocTiming:
    """The declared timing model for one simulation.

    The defaults are the Calendar baseline for every value the sources fix, and
    the minimum for every value they leave open. :meth:`parameters` makes that
    distinction explicit in every report.
    """

    #: Whitepaper §4.4: 64 B per link per cycle, 2 GHz, 128 GB/s per direction.
    link_width_bytes: int = NOC_LINK_WIDTH_BYTES
    clock_hz: float = NOC_CLOCK_HZ
    #: Open item C-5 baseline: 8 bit epoch tag.
    epoch_tag_bits: int = DEFAULT_EPOCH_TAG_BITS
    #: Declared-input: cycles from a flit's link arrival to its availability on
    #: the next links. Includes router pipelining, not the link transfer itself.
    hop_latency_cycles: int = DEFAULT_HOP_LATENCY_CYCLES
    #: Declared-input: flits a core can inject per cycle. MTE3 is a DMA engine;
    #: the design fixes the link rate, not the injection burst rate.
    injection_flits_per_cycle: int = 1
    #: Declared-input: when CalReg releases each member. ``"simultaneous"`` is a
    #: contention stress case. ``"offsets"`` requires an explicit per-source map,
    #: which is exactly what the NoC algorithm's timeslot schedule produces.
    release_policy: str = "simultaneous"
    #: Declared-input: cycles from a landing to the bytes being accounted.
    land_latency_cycles: int = 0

    def __post_init__(self) -> None:
        if self.link_width_bytes <= 0:
            raise WseModelError("link_width_bytes must be positive")
        if self.clock_hz <= 0:
            raise WseModelError("clock_hz must be positive")
        if self.hop_latency_cycles < 1:
            raise WseModelError(
                f"hop_latency_cycles={self.hop_latency_cycles}; a flit cannot "
                "cross a link in less than one cycle"
            )
        if self.injection_flits_per_cycle < 1:
            raise WseModelError("injection_flits_per_cycle must be at least 1")
        if self.release_policy not in ("simultaneous", "offsets"):
            raise WseModelError(
                f"unknown release_policy {self.release_policy!r}; expected "
                "'simultaneous' or 'offsets'"
            )
        if self.land_latency_cycles < 0:
            raise WseModelError("land_latency_cycles must be non-negative")

    def header(self, node_count: int) -> FlitHeader:
        """The per-flit header this model charges (Calendar §1.8)."""
        return FlitHeader(
            route_bits=RouteBits.from_int(0, node_count),
            opcode=1,
            epoch_tag=1,
            epoch_tag_bits=self.epoch_tag_bits,
            link_width_bytes=self.link_width_bytes,
        )

    def parameters(self) -> tuple[TimingParameter, ...]:
        return (
            TimingParameter(
                "link_width_bytes",
                self.link_width_bytes,
                "B",
                ParameterStatus.DOCUMENTED,
                "whitepaper §4.4 / §5.1",
            ),
            TimingParameter(
                "clock_hz",
                self.clock_hz,
                "Hz",
                ParameterStatus.DOCUMENTED,
                "whitepaper §2.2 / §5.1: intra-reticle NoC at 2 GHz",
            ),
            TimingParameter(
                "epoch_tag_bits",
                self.epoch_tag_bits,
                "bit",
                ParameterStatus.BASELINE,
                "open item C-5 baseline (decision 0001)",
            ),
            TimingParameter(
                "hop_latency_cycles",
                self.hop_latency_cycles,
                "cycle",
                ParameterStatus.DECLARED,
                "not fixed by the sources; router pipeline depth is a scenario input",
            ),
            TimingParameter(
                "injection_flits_per_cycle",
                self.injection_flits_per_cycle,
                "flit/cycle",
                ParameterStatus.DECLARED,
                "MTE3 burst rate is not fixed by the sources",
            ),
            TimingParameter(
                "release_policy",
                self.release_policy,
                "-",
                ParameterStatus.DECLARED,
                "CalReg timeslots are the NoC algorithm's output (Calendar §2.7.1)",
            ),
            TimingParameter(
                "land_latency_cycles",
                self.land_latency_cycles,
                "cycle",
                ParameterStatus.DECLARED,
                "accounting latency after a committed payload",
            ),
        )


# -- one send ----------------------------------------------------------------


def flit_byte_profile(*, row_count: int, row_bytes: int, header: FlitHeader) -> tuple[int, ...]:
    """Valid payload bytes of every flit a source injects, in order.

    The source sends ``row_count`` rows of ``row_bytes``; each row is split
    independently, so a row pays its own tail padding (Calendar §1.8). The sum
    is exactly ``row_count * row_bytes`` — the semantics' byte count.
    """
    if row_count < 0 or row_bytes < 0:
        raise WseModelError("row_count and row_bytes must be non-negative")
    per_row = flit_count(row_bytes, header)
    if per_row == 0:
        return ()
    full = header.payload_bytes
    tail = row_bytes - full * (per_row - 1)
    row_profile = (full,) * (per_row - 1) + (tail,)
    return row_profile * row_count


@dataclass(frozen=True)
class TimedSend:
    """One member's AllGather contribution, as a stream of flits."""

    source: int
    route_bits: RouteBits
    flit_bytes: tuple[int, ...]
    release_cycle: int = 0

    def __post_init__(self) -> None:
        if self.source < 0 or self.source >= self.route_bits.node_count:
            raise WseModelError(f"source {self.source} is outside the route bitmap")
        if self.release_cycle < 0:
            raise WseModelError("release_cycle must be non-negative")
        if any(nbytes <= 0 for nbytes in self.flit_bytes):
            raise WseModelError("every flit must carry at least one payload byte")

    @classmethod
    def from_geometry(
        cls,
        *,
        source: int,
        route_bits: RouteBits,
        row_count: int,
        row_bytes: int,
        header: FlitHeader,
        release_cycle: int = 0,
    ) -> TimedSend:
        profile = flit_byte_profile(row_count=row_count, row_bytes=row_bytes, header=header)
        if any(nbytes > header.payload_bytes for nbytes in profile):
            raise WseModelError(
                "flit payload exceeds the link's payload capacity; the header "
                "split is inconsistent with the link width"
            )
        return cls(
            source=source,
            route_bits=route_bits,
            flit_bytes=profile,
            release_cycle=release_cycle,
        )

    @property
    def flits(self) -> int:
        return len(self.flit_bytes)

    @property
    def payload_bytes(self) -> int:
        return sum(self.flit_bytes)


# -- results -----------------------------------------------------------------


@dataclass(frozen=True)
class SourceTiming:
    """What one source's stream did in the mesh."""

    source: int
    release_cycle: int
    flits: int
    payload_bytes: int
    first_injection_cycle: int
    last_injection_cycle: int
    last_landing_cycle: int
    tree_edges: int
    max_depth: int
    landings: int

    def describe(self) -> dict[str, object]:
        return {
            "source": self.source,
            "release_cycle": self.release_cycle,
            "flits": self.flits,
            "payload_bytes": self.payload_bytes,
            "first_injection_cycle": self.first_injection_cycle,
            "last_injection_cycle": self.last_injection_cycle,
            "last_landing_cycle": self.last_landing_cycle,
            "tree_edges": self.tree_edges,
            "max_depth": self.max_depth,
            "landings": self.landings,
        }


@dataclass(frozen=True)
class LinkTiming:
    """Occupancy of one **directed** physical link."""

    tail: int
    head: int
    copies: int
    busy_cycles: int
    first_busy_cycle: int
    last_busy_cycle: int

    @property
    def link(self) -> tuple[int, int]:
        """The undirected physical link, as ``(low, high)``."""
        return (min(self.tail, self.head), max(self.tail, self.head))

    @property
    def span_cycles(self) -> int:
        return self.last_busy_cycle - self.first_busy_cycle + 1

    @property
    def utilization(self) -> float:
        if self.span_cycles <= 0:
            return 0.0
        return self.busy_cycles / self.span_cycles

    def describe(self) -> dict[str, object]:
        return {
            "tail": self.tail,
            "head": self.head,
            "copies": self.copies,
            "busy_cycles": self.busy_cycles,
            "first_busy_cycle": self.first_busy_cycle,
            "last_busy_cycle": self.last_busy_cycle,
            "utilization": round(self.utilization, 4),
        }


@dataclass
class PhaseTiming:
    """The timing result of one collective phase."""

    phase: str
    key_id: int
    opcode: int
    members: tuple[int, ...]
    timing: NocTiming
    sources: tuple[SourceTiming, ...]
    links: tuple[LinkTiming, ...]
    counted_bytes: dict[int, int]
    exp_val: dict[int, int]
    start_cycle: int
    last_landing_cycle: int
    last_event_cycle: int
    lower_bound_cycles: int
    arm_lead_cycles: int = 0
    schedule: str = "simultaneous"
    epoch: int | None = None

    @property
    def transfer_cycles(self) -> int:
        """Cycles from the first release through the last landing, inclusive."""
        return self.last_landing_cycle - self.start_cycle + 1

    @property
    def phase_cycles(self) -> int:
        """``armLeadCycles`` plus the transfer (Calendar §1.5)."""
        return self.arm_lead_cycles + self.transfer_cycles

    @property
    def seconds(self) -> float:
        return self.phase_cycles / self.timing.clock_hz

    @property
    def lower_bound_seconds(self) -> float:
        return (self.arm_lead_cycles + self.lower_bound_cycles) / self.timing.clock_hz

    @property
    def complete(self) -> bool:
        return all(
            self.counted_bytes.get(node, 0) == expected for node, expected in self.exp_val.items()
        )

    @property
    def bottleneck_links(self) -> tuple[LinkTiming, ...]:
        """The busiest directed links, most loaded first."""
        return tuple(
            sorted(self.links, key=lambda link: (-link.busy_cycles, link.tail, link.head))[:8]
        )

    @property
    def peak_directed_link_copies(self) -> int:
        return max((link.copies for link in self.links), default=0)

    @property
    def peak_link_utilization(self) -> float:
        return max((link.utilization for link in self.links), default=0.0)

    @property
    def total_flit_hops(self) -> int:
        return sum(link.copies for link in self.links)

    def check(self) -> None:
        """Raise unless every member's counted bytes reached its ``expVal``."""
        if not self.complete:
            detail = "; ".join(
                f"node {node} expected {expected} B, counted {self.counted_bytes.get(node, 0)} B"
                for node, expected in sorted(self.exp_val.items())
                if self.counted_bytes.get(node, 0) != expected
            )
            raise CalendarError(f"phase {self.phase}: {detail}")

    def describe(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "key_id": self.key_id,
            "opcode": self.opcode,
            "epoch": self.epoch,
            "members": list(self.members),
            "member_count": len(self.members),
            "schedule": self.schedule,
            "arm_lead_cycles": self.arm_lead_cycles,
            "transfer_cycles": self.transfer_cycles,
            "phase_cycles": self.phase_cycles,
            "seconds": self.seconds,
            "lower_bound_cycles": self.lower_bound_cycles,
            "lower_bound_seconds": self.lower_bound_seconds,
            "complete": self.complete,
            "total_flit_hops": self.total_flit_hops,
            "peak_directed_link_copies": self.peak_directed_link_copies,
            "peak_link_utilization": round(self.peak_link_utilization, 4),
            "bottleneck_links": [link.describe() for link in self.bottleneck_links],
            "timing_parameters": [parameter.describe() for parameter in self.timing.parameters()],
            "sources": [source.describe() for source in self.sources],
        }


@dataclass
class TwoPhaseTiming:
    """The FFN example's two sequential AllGather phases."""

    phases: tuple[PhaseTiming, ...]

    @property
    def transfer_cycles(self) -> int:
        return sum(phase.transfer_cycles for phase in self.phases)

    @property
    def phase_cycles(self) -> int:
        return sum(phase.phase_cycles for phase in self.phases)

    @property
    def seconds(self) -> float:
        return sum(phase.seconds for phase in self.phases)

    @property
    def lower_bound_cycles(self) -> int:
        return sum(phase.lower_bound_cycles + phase.arm_lead_cycles for phase in self.phases)

    def check(self) -> None:
        for phase in self.phases:
            phase.check()

    def describe(self) -> dict[str, object]:
        return {
            "phases": [phase.describe() for phase in self.phases],
            "transfer_cycles": self.transfer_cycles,
            "phase_cycles": self.phase_cycles,
            "seconds": self.seconds,
            "lower_bound_cycles": self.lower_bound_cycles,
        }


# -- the simulator -----------------------------------------------------------


@dataclass(frozen=True)
class _TreePlan:
    source: int
    lands: dict[int, bool]
    out_edges: dict[int, tuple[tuple[int, int], ...]]
    depth: dict[int, int]
    edges: frozenset[tuple[int, int]]
    max_depth: int
    landings: int


def _tree_plan(topology: MeshTopology, send: TimedSend) -> _TreePlan:
    trace = simulate_multicast(topology, send.route_bits, source=send.source)
    if not trace.is_clean_tree_walk:
        raise CalendarError(
            f"source {send.source}: the {{P=1}} induced subgraph is not a tree "
            f"(duplicate landings {list(trace.duplicate_nodes)}, revisits "
            f"{list(trace.unexpanded_revisits)}); timing a non-terminating "
            "forwarding tree is undefined (Calendar §2.2)"
        )
    lands: dict[int, bool] = {}
    out_edges: dict[int, tuple[tuple[int, int], ...]] = {}
    depth: dict[int, int] = {send.source: 0}
    edges: set[tuple[int, int]] = set()
    for hop in trace.hops:
        lands[hop.node] = hop.lands
        outgoing = tuple((hop.node, peer) for peer in hop.egress)
        out_edges[hop.node] = outgoing
        edges.update(outgoing)
        for peer in hop.egress:
            depth[peer] = depth[hop.node] + 1
    return _TreePlan(
        source=send.source,
        lands=lands,
        out_edges=out_edges,
        depth=depth,
        edges=frozenset(edges),
        max_depth=trace.max_depth,
        landings=sum(1 for hop in trace.hops if hop.lands),
    )


class _Simulator:
    """Deterministic discrete-event simulation of the passive mesh."""

    def __init__(self, topology: MeshTopology, timing: NocTiming) -> None:
        self.topology = topology
        self.timing = timing
        self.next_free: dict[tuple[int, int], int] = {}
        self.queues: dict[tuple[int, int], list[tuple[int, int, int, int, int]]] = {}
        self.pump_at: dict[tuple[int, int], int | None] = {}
        self.copies: dict[tuple[int, int], int] = {}
        self.busy: dict[tuple[int, int], int] = {}
        self.first_busy: dict[tuple[int, int], int] = {}
        self.last_busy: dict[tuple[int, int], int] = {}
        self.counted: dict[int, int] = {}
        self.landing_at: dict[int, int] = {}
        self.landing_count: dict[int, int] = {}
        self.source_last_landing: dict[int, int] = {}
        self.source_landings: dict[int, int] = {}
        self.events: list[tuple[int, int, int, object]] = []
        self.plans: dict[int, _TreePlan] = {}
        self._sequence = 0

    # -- event helpers -------------------------------------------------------

    def _push(self, time: int, kind: int, payload: object) -> None:
        self._sequence += 1
        heapq.heappush(self.events, (time, kind, self._sequence, payload))

    def _link(self, edge: tuple[int, int]) -> None:
        self.next_free.setdefault(edge, 0)
        self.queues.setdefault(edge, [])
        self.pump_at.setdefault(edge, None)
        self.copies.setdefault(edge, 0)
        self.busy.setdefault(edge, 0)
        self.first_busy.setdefault(edge, 0)
        self.last_busy.setdefault(edge, 0)

    def _enqueue(
        self,
        edge: tuple[int, int],
        available: int,
        source: int,
        flit: int,
        nbytes: int,
        depth: int,
    ) -> None:
        self._link(edge)
        heapq.heappush(self.queues[edge], (available, source, flit, nbytes, depth))
        self.copies[edge] += 1
        self._schedule(edge, available)

    def _schedule(self, edge: tuple[int, int], available: int) -> None:
        desired = max(available, self.next_free[edge])
        current = self.pump_at[edge]
        if current is None or desired < current:
            self.pump_at[edge] = desired
            self._push(desired, 1, edge)

    def _inject(self, time: int, plan: _TreePlan, flit: int, nbytes: int) -> None:
        for edge in plan.out_edges[plan.source]:
            self._enqueue(edge, time, plan.source, flit, nbytes, 0)
        if plan.lands.get(plan.source, False):
            landed = time + self.timing.land_latency_cycles
            self._land(plan, plan.source, landed, nbytes)

    def _land(self, plan: _TreePlan, node: int, time: int, nbytes: int) -> None:
        self.counted[node] = self.counted.get(node, 0) + nbytes
        self.landing_count[node] = self.landing_count.get(node, 0) + 1
        previous = self.landing_at.get(node, -1)
        if time > previous:
            self.landing_at[node] = time
        previous_source = self.source_last_landing.get(plan.source, -1)
        if time > previous_source:
            self.source_last_landing[plan.source] = time
        self.source_landings[plan.source] = self.source_landings.get(plan.source, 0) + 1

    def _pump(self, time: int, edge: tuple[int, int]) -> None:
        if self.pump_at[edge] != time:
            return  # superseded by an earlier pump
        self.pump_at[edge] = None
        queue = self.queues[edge]
        if not queue:
            return
        available, source, flit, nbytes, depth = heapq.heappop(queue)
        start = max(time, available, self.next_free[edge])
        self.next_free[edge] = start + 1
        self.busy[edge] += 1
        if self.busy[edge] == 1:
            self.first_busy[edge] = start
        self.last_busy[edge] = start
        self._push(start + self.timing.hop_latency_cycles, 2, (edge, source, flit, nbytes, depth))
        if queue:
            self._schedule(edge, queue[0][0])

    def _complete(
        self, time: int, edge: tuple[int, int], source: int, flit: int, nbytes: int, depth: int
    ) -> None:
        plan = self.plans[source]
        head = edge[1]
        if plan.lands.get(head, False):
            self._land(plan, head, time + self.timing.land_latency_cycles, nbytes)
        for child in plan.out_edges.get(head, ()):
            self._enqueue(child, time, source, flit, nbytes, depth + 1)

    # -- driver --------------------------------------------------------------

    def run(self, sends: tuple[TimedSend, ...]) -> tuple[PhaseTiming, tuple[SourceTiming, ...]]:
        self.plans = {send.source: _tree_plan(self.topology, send) for send in sends}
        for send in sends:
            for flit, nbytes in enumerate(send.flit_bytes):
                injection = send.release_cycle + flit // self.timing.injection_flits_per_cycle
                self._push(injection, 0, (send.source, flit, nbytes))
        last_event = 0
        while self.events:
            time, kind, _, payload = heapq.heappop(self.events)
            last_event = max(last_event, time)
            if kind == 0:
                source, flit, nbytes = payload  # type: ignore[misc]
                self._inject(time, self.plans[source], flit, nbytes)
            elif kind == 1:
                self._pump(time, payload)  # type: ignore[arg-type]
            else:
                edge, source, flit, nbytes, depth = payload  # type: ignore[misc]
                self._complete(time, edge, source, flit, nbytes, depth)
        return self._result(sends, last_event), self._source_timings(sends)

    def _source_timings(self, sends: tuple[TimedSend, ...]) -> tuple[SourceTiming, ...]:
        out: list[SourceTiming] = []
        for send in sends:
            plan = self.plans[send.source]
            if send.flits:
                last_injection = (
                    send.release_cycle + (send.flits - 1) // self.timing.injection_flits_per_cycle
                )
            else:
                last_injection = send.release_cycle
            out.append(
                SourceTiming(
                    source=send.source,
                    release_cycle=send.release_cycle,
                    flits=send.flits,
                    payload_bytes=send.payload_bytes,
                    first_injection_cycle=send.release_cycle,
                    last_injection_cycle=last_injection,
                    last_landing_cycle=self.source_last_landing.get(send.source, -1),
                    tree_edges=len(plan.edges),
                    max_depth=plan.max_depth,
                    landings=self.source_landings.get(send.source, 0),
                )
            )
        return tuple(out)

    def _expected_bytes(self, sends: tuple[TimedSend, ...]) -> dict[int, int]:
        """Bytes each destination should count, derived from the tree plans.

        This is the timing model's own conservation check: it must equal the
        compiled ``expVal`` for every AllGather member.
        """
        expected: dict[int, int] = {}
        for send in sends:
            plan = self.plans[send.source]
            for node, lands in plan.lands.items():
                if lands:
                    expected[node] = expected.get(node, 0) + send.payload_bytes
        return expected

    def _result(self, sends: tuple[TimedSend, ...], last_event: int) -> PhaseTiming:
        members = tuple(sorted(send.source for send in sends))
        start = min((send.release_cycle for send in sends), default=0)
        last_landing = max(self.landing_at.values(), default=start - 1)
        links = tuple(
            LinkTiming(
                tail=edge[0],
                head=edge[1],
                copies=self.copies[edge],
                busy_cycles=self.busy[edge],
                first_busy_cycle=self.first_busy[edge],
                last_busy_cycle=self.last_busy[edge],
            )
            for edge in sorted(self.copies)
            if self.copies[edge]
        )
        return PhaseTiming(
            phase="",
            key_id=0,
            opcode=0,
            members=members,
            timing=self.timing,
            sources=(),
            links=links,
            counted_bytes=dict(self.counted),
            exp_val=self._expected_bytes(sends),
            start_cycle=start,
            last_landing_cycle=last_landing,
            last_event_cycle=last_event,
            lower_bound_cycles=_lower_bound(sends, links, self.plans, self.timing),
        )


def _lower_bound(
    sends: tuple[TimedSend, ...],
    links: tuple[LinkTiming, ...],
    plans: dict[int, _TreePlan],
    timing: NocTiming,
) -> int:
    """The schedule-independent floor, in cycles.

    Three independent constraints: the busiest directed link (one flit per
    cycle), the largest source stream (one injected flit per cycle), and the
    deepest tree path (one hop latency per level).
    """
    # The last flit on a saturated link enters at ``copies - 1`` and needs
    # ``hop_latency`` more cycles to reach the head, so the inclusive span is
    # ``copies + hop_latency`` when the link starts at cycle 0.
    link_floor = max((link.copies for link in links), default=0)
    if link_floor:
        link_floor += timing.hop_latency_cycles
    injection_floor = max(
        ((send.flits - 1) // timing.injection_flits_per_cycle + 1 for send in sends if send.flits),
        default=0,
    )
    depth_floor = max(
        (plan.max_depth * timing.hop_latency_cycles + 1 for plan in plans.values()),
        default=0,
    )
    return max(link_floor, injection_floor, depth_floor)


# -- public entry points -----------------------------------------------------


def simulate_phase(
    topology: MeshTopology,
    sends: tuple[TimedSend, ...],
    *,
    timing: NocTiming | None = None,
) -> PhaseTiming:
    """Time one phase's sends on ``topology`` under ``timing``.

    The sends are the already-compiled ``{P, L}`` bitmaps; this function never
    invents a route. It raises for a non-tree induced subgraph, a source outside
    the mesh, or a flit stream that does not fit the link width.
    """
    if not sends:
        raise WseModelError("a timing phase needs at least one send")
    chosen = timing or NocTiming()
    for send in sends:
        if send.route_bits.node_count != topology.node_count:
            raise CalendarError(
                f"source {send.source}: routeBits covers "
                f"{send.route_bits.node_count} nodes but topology "
                f"{topology.name} has {topology.node_count}"
            )
    simulator = _Simulator(topology, chosen)
    result, sources = simulator.run(tuple(sorted(sends, key=lambda send: send.source)))
    result.sources = sources
    return result


def time_allgather(
    noc: Noc,
    table: CalendarRouteTable,
    key_id: int,
    *,
    geometry: PayloadGeometry,
    groups: dict[int, tuple[int, ...]],
    timing: NocTiming | None = None,
    release_offsets: dict[int, int] | None = None,
    epoch: int | None = None,
) -> PhaseTiming:
    """Time one AllGather phase from the compiled table.

    Semantics are the same as :func:`wse_model.collective.run_allgather`: every
    member is checked against the compiled key, ``self_delivery`` must agree,
    and each source's ``CalReg[opcode]`` must be installed. Timing then uses the
    table's bitmaps and the geometry's flit profile.
    """
    chosen = timing or NocTiming()
    definition = table.key(key_id)
    if definition.collective is not Collective.ALL_GATHER:
        raise CalendarError(
            f"keyId {key_id} is {definition.collective.name}; timing closes for "
            "AllGather only (Reduce semantics are open item C-10)"
        )
    if table.topology.node_count != noc.topology.node_count:
        raise CalendarError(
            f"table topology has {table.topology.node_count} nodes but the NoC "
            f"has {noc.topology.node_count}"
        )
    if noc.self_delivery != definition.self_delivery:
        raise CalendarError(
            f"the NoC's self_delivery={noc.self_delivery} disagrees with the "
            f"compiled key ({definition.self_delivery}); the source's own L bit "
            "would be inconsistent (Calendar §2.7.2)"
        )
    members = tuple(sorted(groups))
    for node in members:
        if node not in definition.member_nodes:
            raise CalendarError(
                f"node {node} participates in phase {definition.route_key.phase_id} "
                "but is not a member of the compiled key"
            )
        if node not in noc.banks:
            raise CalendarError(f"node {node} has no CalReg bank")
        # Fail closed on an uninstalled or reserved opcode, exactly as a send does.
        noc.banks[node].lookup(definition.opcode)

    if release_offsets is not None:
        missing = sorted(set(members) - set(release_offsets))
        if missing:
            raise CalendarError(
                f"release_policy 'offsets' needs an offset for every member; missing {missing}"
            )
        if chosen.release_policy != "offsets":
            raise CalendarError(
                "release_offsets were supplied but timing.release_policy is "
                f"{chosen.release_policy!r}; set it to 'offsets'"
            )
    elif chosen.release_policy == "offsets":
        raise CalendarError(
            "release_policy 'offsets' requires an explicit release_offsets map; "
            "the CalReg timeslot schedule is the NoC algorithm's output "
            "(Calendar §2.7.1)"
        )

    header = chosen.header(table.topology.node_count)
    sends = tuple(
        TimedSend.from_geometry(
            source=node,
            route_bits=definition.entry(node).route_bits,
            row_count=geometry.row_count,
            row_bytes=geometry.row_bytes,
            header=header,
            release_cycle=0 if release_offsets is None else release_offsets[node],
        )
        for node in members
    )

    phase = simulate_phase(table.topology, sends, timing=chosen)
    land_counts = definition.land_counts()
    phase.phase = definition.route_key.phase_id
    phase.key_id = key_id
    phase.opcode = definition.opcode
    phase.epoch = epoch
    phase.arm_lead_cycles = definition.arm_lead_cycles
    phase.schedule = chosen.release_policy
    phase.exp_val = {node: geometry.exp_val(land_counts.get(node, 0)) for node in members}
    return phase


def time_two_phase_allgather(
    example: object,
    *,
    timing: NocTiming | None = None,
    release_offsets: dict[int, dict[int, int]] | None = None,
) -> TwoPhaseTiming:
    """Time the FFN example's ROW then COL AllGather phases.

    ``example`` is a :class:`wse_model.fixtures.FFNExample`. The two phases share
    ``opcode 1``, so Calendar §1.5 gives them epochs 1 and 2 on the same counter.
    """
    from wse_model.fixtures import FFNExample

    if not isinstance(example, FFNExample):
        raise WseModelError("time_two_phase_allgather expects an FFNExample")
    noc = Noc(example.table.topology, calreg=example.calreg())
    phases = []
    for key_id, epoch in ((0, 1), (1, 2)):
        offsets = None if release_offsets is None else release_offsets.get(key_id)
        phase = time_allgather(
            noc,
            example.table,
            key_id,
            geometry=example.geometry(key_id),
            groups=example.groups(key_id),
            timing=timing,
            release_offsets=offsets,
            epoch=epoch,
        )
        phases.append(phase)
    return TwoPhaseTiming(phases=tuple(phases))
