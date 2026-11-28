"""Address-free workload descriptions for the generated CoarseWseMachine.

The adapter validates topology and emits metadata loading records only. Runtime
resource arbitration and completion timestamps belong to the generated model.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

NODES = 40
ALL_NODES = (1 << NODES) - 1
DIRECTIONS = (-8, -1, 1, 8)
PARAMETERS = (
    "read_first",
    "write_first",
    "numerator",
    "denominator",
    "width",
    "flit_bytes",
    "hop_ticks",
    "slot_ticks",
    "tx_ticks",
    "rx_ticks",
    "barrier_ticks",
    "capacity",
    "guard_ticks",
)


def direction(source: int, destination: int) -> int:
    if not 0 <= source < NODES or not 0 <= destination < NODES:
        raise ValueError("node must be in the fixed 5x8 grid")
    delta = destination - source
    if delta not in DIRECTIONS or (abs(delta) == 1 and source // 8 != destination // 8):
        raise ValueError(f"edge {source}->{destination} is not a physical grid link")
    return DIRECTIONS.index(delta)


@dataclass(frozen=True)
class Route:
    id: int
    source: int
    edges: tuple[tuple[int, int], ...]
    targets: tuple[int, ...]

    def validate(self) -> None:
        if not 0 <= self.id < 256 or not 0 <= self.source < NODES:
            raise ValueError("route ID or source outside model capacity")
        if not self.targets or len(set(self.targets)) != len(self.targets):
            raise ValueError("route needs unique receive targets")
        parents: dict[int, int] = {}
        children: dict[int, list[int]] = {}
        for source, dest in self.edges:
            direction(source, dest)
            if dest in parents or dest == self.source:
                raise ValueError("route must be a directed tree rooted at source")
            parents[dest] = source
            children.setdefault(source, []).append(dest)
        reached = {self.source}
        pending = [self.source]
        while pending:
            for child in children.get(pending.pop(), ()):
                if child in reached:
                    raise ValueError("route contains a cycle")
                reached.add(child)
                pending.append(child)
        if set(parents) - reached or any(target not in reached for target in self.targets):
            raise ValueError("route edges and targets must be reachable from source")


@dataclass(frozen=True)
class Transfer:
    id: int
    source_node: int
    route_id: int
    nbytes: int
    logical_block_id: int = 0
    release_time: int = 0
    dependency_group: int | None = None
    completion_group: int = 0


@dataclass(frozen=True)
class WaitGroup:
    group: int


@dataclass(frozen=True)
class Barrier:
    pass


@dataclass(frozen=True)
class RetireGroup:
    group: int


@dataclass
class Profile:
    read_first: int = 200
    write_first: int = 200
    numerator: int = 10
    denominator: int = 7
    width: int = 128
    flit_bytes: int = 52
    hop_ticks: int = 1
    slot_ticks: int = 1024
    tx_ticks: int = 2
    rx_ticks: int = 2
    barrier_ticks: int = 24
    capacity: int = 65536
    guard_ticks: int = 1
    calendar: dict[tuple[int, int, int], int] = field(default_factory=dict)
    name: str = "compatible-endpoints-open-calendar"

    @classmethod
    def open(cls, **overrides: int) -> Profile:
        values = {
            "read_first": 0,
            "write_first": 0,
            "numerator": 0,
            "tx_ticks": 0,
            "rx_ticks": 0,
            "barrier_ticks": 0,
            "guard_ticks": 0,
            "name": "open-zero-endpoint",
        }
        values.update(overrides)
        return cls(**values)

    def validate(self) -> None:
        for name in PARAMETERS:
            value = getattr(self, name)
            if not isinstance(value, int) or not 0 <= value < 1 << 64:
                raise ValueError(f"invalid {name}")
        if min(self.denominator, self.width, self.flit_bytes, self.slot_ticks, self.capacity) == 0:
            raise ValueError(
                "service divisor, width, flit size, slot and capacity must be positive"
            )
        if self.width >= 1 << 32 or self.flit_bytes >= 1 << 32:
            raise ValueError("widths must fit the model's byte-count type")
        for (node, port, slot), mask in self.calendar.items():
            if not (
                0 <= node < NODES and 0 <= port < 4 and 0 <= slot < 8 and 0 <= mask <= ALL_NODES
            ):
                raise ValueError("invalid Calendar entry")


Command = Transfer | WaitGroup | Barrier | RetireGroup


def _encode_transfer(command: Transfer, route: Route) -> int:
    fields = (
        (1, 4),
        (command.source_node, 6),
        (command.id, 16),
        (command.nbytes, 32),
        (sum(1 << target for target in route.targets), 40),
        (command.route_id, 8),
        (255 if command.dependency_group is None else command.dependency_group, 8),
        (command.completion_group, 8),
        (command.release_time, 64),
        (command.logical_block_id, 32),
    )
    result = shift = 0
    for value, width in fields:
        if not 0 <= value < 1 << width:
            raise ValueError("transfer field outside finite model representation")
        result |= value << shift
        shift += width
    return result


@dataclass
class Workload:
    routes: Iterable[Route]
    commands: Iterable[Command]
    profile: Profile = field(default_factory=Profile)

    def records(self) -> list[tuple[int, int, int]]:
        self.profile.validate()
        routes = tuple(self.routes)
        commands = tuple(self.commands)
        by_id = {route.id: route for route in routes}
        if len(by_id) != len(routes):
            raise ValueError("route IDs must be unique")
        for route in routes:
            route.validate()
        if len(commands) + 1 > 1024:
            raise ValueError("program capacity is 1024 metadata commands")
        program: list[int] = []
        expected: dict[int, int] = {}
        identities: set[int] = set()
        for command in commands:
            if isinstance(command, Transfer):
                if command.id in identities:
                    raise ValueError("transfer IDs must be unique")
                identities.add(command.id)
                route = by_id[command.route_id]
                if route.source != command.source_node or command.nbytes <= 0:
                    raise ValueError("transfer source/route or byte count mismatch")
                if (
                    command.nbytes + self.profile.flit_bytes - 1
                ) // self.profile.flit_bytes > 65535:
                    raise ValueError("packet exceeds the model's 16-bit flit count")
                if not 0 <= command.completion_group < 255:
                    raise ValueError("completion groups must be in 0..254")
                if command.dependency_group is not None and not 0 <= command.dependency_group < 255:
                    raise ValueError("dependency group must be in 0..254")
                expected[command.completion_group] = expected.get(
                    command.completion_group, 0
                ) + len(route.targets)
                program.append(_encode_transfer(command, route))
            elif isinstance(command, Barrier):
                program.append(3)
            elif isinstance(command, WaitGroup | RetireGroup):
                if not 0 <= command.group < 255:
                    raise ValueError("group must be in 0..254")
                program.append(
                    (2 if isinstance(command, WaitGroup) else 4) | (command.group << 114)
                )
            else:
                raise TypeError(f"unsupported coarse command {type(command).__name__}")
        groups = set(expected)
        for command in commands:
            if isinstance(command, Transfer) and command.dependency_group is not None:
                if (
                    command.dependency_group not in groups
                    or command.dependency_group == command.completion_group
                ):
                    raise ValueError("dependency must name another defined completion group")
            if isinstance(command, WaitGroup | RetireGroup) and command.group not in groups:
                raise ValueError("wait/retire must name a defined completion group")
        program.append(5)
        records = [(6, index, getattr(self.profile, name)) for index, name in enumerate(PARAMETERS)]
        for node in range(NODES):
            for port in range(4):
                for slot in range(8):
                    mask = self.profile.calendar.get((node, port, slot), ALL_NODES)
                    records.append((3, (node * 4 + port) * 8 + slot, mask))
        for route in routes:
            masks = [0] * NODES
            for source, dest in route.edges:
                masks[source] |= 1 << direction(source, dest)
            records.extend((2, route.id * NODES + node, mask) for node, mask in enumerate(masks))
        records.extend((4, group, total) for group, total in sorted(expected.items()))
        records.extend((1, index, word) for index, word in enumerate(program))
        records.append((5, 0, len(program)))
        return records

    def write(self, path: Path | str) -> Path:
        """Write loader.bin plus a human-readable description beside it."""
        path = Path(path)
        if path.suffix != ".bin":
            path.mkdir(parents=True, exist_ok=True)
            path = path / "loader.bin"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.routes = tuple(self.routes)
        self.commands = tuple(self.commands)
        data = b"".join(
            struct.pack("<HH", kind, index) + value.to_bytes(64, "little")
            for kind, index, value in self.records()
        )
        path.write_bytes(data)
        profile = {name: getattr(self.profile, name) for name in PARAMETERS}
        profile.update(
            name=self.profile.name,
            calendar=[[*key, value] for key, value in sorted(self.profile.calendar.items())],
        )
        description = {
            "schema": "wse-coarse-workload/1",
            "profile": profile,
            "routes": [asdict(route) for route in self.routes],
            "commands": [
                dict(kind=type(command).__name__, **asdict(command)) for command in self.commands
            ],
            "loader_sha256": hashlib.sha256(data).hexdigest(),
        }
        path.with_suffix(".json").write_text(json.dumps(description, indent=2) + "\n")
        return path
