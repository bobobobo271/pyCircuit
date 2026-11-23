#!/usr/bin/env python3
"""Build and time the FFN AllGather on the pyCircuit C++ backend."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from wse_model.calendar.route_bits import RouteBits
from wse_model.fixtures import ffn_example
from wse_model.noc import NocTiming, PhaseTiming, TimedSend
from wse_model.noc.flit import flit_count
from wse_model.noc.timing import simulate_phase
from wse_model.topology import CALENDAR_BASELINE, WHITEPAPER_HARDWARE

PYCIRCUIT_REVISION = "1817b4c6ee2f3e9c088ccbc21b186d775e50b6f2"
HOP_SOURCE = Path(__file__).resolve().parents[1] / "src/wse_model/pycircuit/allgather_line.py"


@dataclass(frozen=True)
class Phase:
    reference: PhaseTiming
    group: tuple[int, ...]
    sends: dict[int, TimedSend]
    flits_per_row: int
    full_bytes: int
    calreg: int


def scenarios(nodes: int, timing: NocTiming) -> list[Phase]:
    example = ffn_example()
    topology = (CALENDAR_BASELINE if nodes == 40 else WHITEPAPER_HARDWARE).topology
    header = timing.header(nodes)
    calreg = sum(1 << opcode for opcode in example.calreg().opcodes)
    phases = []
    for key_id in (0, 1):
        definition = example.table.key(key_id)
        geometry = example.geometry(key_id)
        groups = sorted(set(example.groups(key_id).values()))
        sends = {
            node: TimedSend.from_geometry(
                source=node,
                route_bits=RouteBits.from_int(definition.entry(node).route_bits.to_int(), nodes),
                row_count=geometry.row_count,
                row_bytes=geometry.row_bytes,
                header=header,
            )
            for node in sorted(definition.member_nodes)
        }
        # Each phase is a set of independent, identically loaded lines. Check
        # the fixture before substituting one representative for all of them.
        members = [node for group in groups for node in group]
        if sorted(members) != list(sends):
            raise ValueError("AllGather groups must partition the members")
        for group in groups:
            for rank, node in enumerate(group):
                adjacent = tuple(peer for peer in topology.neighbours(node) if peer in group)
                if adjacent != tuple(group[i] for i in (rank - 1, rank + 1) if 0 <= i < len(group)):
                    raise ValueError(f"group {group} is not a physical line")
                route = sends[node].route_bits
                if route.pass_nodes != set(group) or route.land_nodes != set(group) - {node}:
                    raise ValueError(f"node {node} does not carry its full line's route")
        if len({len(group) for group in groups}) != 1:
            raise ValueError("representative timing requires equal-size groups")

        reference = simulate_phase(topology, tuple(sends.values()), timing=timing)
        reference.check()
        if reference.exp_val != {node: definition.entry(node).expected_rx_bytes for node in sends}:
            raise ValueError("timed payload disagrees with compiled expVal")
        if definition.arm_lead_cycles is None:
            raise ValueError("the FFN scenario must supply armLeadCycles")
        reference.phase = definition.route_key.phase_id
        reference.opcode = definition.opcode
        reference.epoch = key_id + 1
        reference.arm_lead_cycles = definition.arm_lead_cycles
        phases.append(
            Phase(
                reference,
                groups[0],
                sends,
                flit_count(geometry.row_bytes, header),
                header.payload_bytes,
                calreg,
            )
        )
    return phases


def write_system(source: Path, nodes: int, phases: list[Phase]) -> None:
    source.mkdir(parents=True, exist_ok=True)
    model = HOP_SOURCE.read_text(encoding="utf-8")
    model = model.replace(
        "import pycircuit as ac\n", f"import pycircuit as ac\n\nROUTE_WIDTH = {2 * nodes}\n", 1
    )
    lines = [model, "\n@ac.system\ndef AllGather():"]
    for phase in phases:
        reference = phase.reference
        for direction, group in enumerate((phase.group, phase.group[::-1])):
            incoming = "Flit()"
            for rank, node in enumerate(group):
                downstream = group[rank + 1] if rank + 1 < len(group) else node
                send = phase.sends[node]
                outgoing = f"link_{reference.epoch}_{direction}_{node}"
                lines.append(
                    f"    {outgoing} = Hop({incoming}, {node}, {downstream}, "
                    f"{send.route_bits.to_int()}, {reference.opcode}, {reference.epoch}, "
                    f"{phase.calreg}, {phase.flits_per_row}, {send.flits}, "
                    f"{phase.full_bytes}, {send.flit_bytes[-1]})"
                )
                incoming = outgoing
    (source / "allgather.py").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_system(root: Path, toolchain: Path, cycles: int) -> Path:
    driver = [sys.executable, str(toolchain / "bin/pycircuit")]
    units, final, cpp, build = (root / name for name in ("units", "allgather.ac", "cpp", "build"))
    package = toolchain / "share/pycircuit/cmake"
    commands = [
        [
            *driver,
            "compile",
            "-c",
            "allgather.py",
            "--source-root",
            root / "source",
            "--package-prefix",
            "wse_timing",
            "-o",
            units,
            "--replace",
        ],
        [
            *driver,
            "link",
            units,
            "--top",
            "wse_timing.allgather.AllGather",
            "-o",
            final,
            "--replace",
        ],
        [*driver, "emit", final, "--target", "cpp", "-o", cpp, "--replace"],
        [
            "cmake",
            "-G",
            "Ninja",
            "-S",
            cpp,
            "-B",
            build,
            "-DCMAKE_BUILD_TYPE=Release",
            f"-Dpycircuit_DIR={package}",
        ],
        ["cmake", "--build", build, "--target", "pycircuit_sim"],
    ]
    build_log = root / "build.log"
    with build_log.open("wb") as log:
        for command in commands:
            if subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode:
                raise RuntimeError(f"{command[0]} build step failed; see {build_log}")
    simulator = build / "bin" / ("pycircuit_sim.exe" if os.name == "nt" else "pycircuit_sim")
    trace = root / "trace.jsonl"
    subprocess.run([simulator, "--cycles", str(cycles), "--events", trace], check=True)
    return trace


def measure(output: Path, phase: Phase) -> dict[str, object]:
    reference = phase.reference
    arrivals: dict[tuple[int, int, int], int] = {}
    copies: Counter[tuple[int, int]] = Counter()
    send_cycles: set[int] = set()
    landing_cycles: dict[int, int] = {}
    with output.open(encoding="utf-8") as trace:
        for line in trace:
            if not line.startswith("{"):
                continue
            event = json.loads(line)
            if event["kind"] != "log":
                continue
            sample = int(event["evaluation_epoch"])
            if sample % 2 == 0:  # The generated runner drives rising edges on odd epochs.
                continue
            epoch, *values = (int(value["value"]) for value in event["values"])
            if epoch != reference.epoch:
                continue
            cycle = sample // 2
            if event["spec"]["event"] == "wse_link":
                tail, head = values
                copies[tail, head] += 1
                send_cycles.add(cycle)
            elif event["spec"]["event"] == "wse_land":
                node, source, sequence, nbytes = values
                key = (node, source, sequence)
                if key in arrivals:
                    raise ValueError(f"duplicate landing {key} in epoch {epoch}")
                arrivals[key] = nbytes
                landing_cycles[node] = cycle

    expected = {
        (node, source, sequence): nbytes
        for node in phase.group
        for source in phase.group
        if phase.sends[source].route_bits.pair(node).lands
        for sequence, nbytes in enumerate(phase.sends[source].flit_bytes)
    }
    if arrivals != expected:
        missing = len(expected.keys() - arrivals.keys())
        extra = len(arrivals.keys() - expected.keys())
        wrong = sum(arrivals[key] != expected[key] for key in arrivals.keys() & expected.keys())
        raise ValueError(f"epoch {reference.epoch} flits differ ({missing=} {extra=} {wrong=})")
    expected_links = {
        (link.tail, link.head): link.copies
        for link in reference.links
        if link.tail in phase.group and link.head in phase.group
    }
    if copies != expected_links:
        raise ValueError(
            f"epoch {reference.epoch} directed link loads differ from the semantic core"
        )
    transfer = max(landing_cycles.values()) - min(send_cycles) + 1
    if transfer != reference.transfer_cycles:
        raise ValueError(
            f"epoch {reference.epoch}: pyCircuit {transfer}, core {reference.transfer_cycles}"
        )
    return {
        "phase": reference.phase,
        "group": phase.group,
        "epoch": reference.epoch,
        "flits_per_source": phase.sends[phase.group[0]].flits,
        "flits_on_busiest_link": max(copies.values()),
        "received_bytes_per_member": reference.exp_val[phase.group[0]],
        "transfer_cycles": transfer,
        "arm_lead_cycles": reference.arm_lead_cycles,
        "total_cycles": transfer + reference.arm_lead_cycles,
        "total_ns": (transfer + reference.arm_lead_cycles) * 1e9 / reference.timing.clock_hz,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", required=True, type=Path, help="installed pyCircuit prefix")
    parser.add_argument("--nodes", choices=(40, 48), type=int, default=40)
    parser.add_argument("--output", type=Path, default=Path(".wse_model_out/pycircuit-allgather"))
    args = parser.parse_args()
    timing = NocTiming(hop_latency_cycles=1, injection_flits_per_cycle=1, land_latency_cycles=0)
    phases = scenarios(args.nodes, timing)
    root = args.output.resolve() / f"nodes-{args.nodes}"
    source = root / "source"
    toolchain = args.toolchain.resolve()
    metadata_path = toolchain / "share/pycircuit/toolchain-metadata.json"
    metadata = (
        json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    )
    # Total link work provides a run limit independent of the expected latency.
    cycles = (
        max(
            sum(link.copies for link in phase.reference.links if link.tail in phase.group)
            for phase in phases
        )
        + 1
    )
    write_system(source, args.nodes, phases)
    (root / "timing.json").unlink(missing_ok=True)
    trace = run_system(root, toolchain, cycles)
    reports = [measure(trace, phase) for phase in phases]
    transfer_cycles = sum(int(report["transfer_cycles"]) for report in reports)
    total_cycles = sum(int(report["total_cycles"]) for report in reports)
    result = {
        "pycircuit_reference_revision": PYCIRCUIT_REVISION,
        "pycircuit_toolchain_revision": metadata.get("git_sha"),
        "nodes": args.nodes,
        "timing_parameters": [parameter.describe() for parameter in timing.parameters()],
        "link_arbitration": "forwarded flits before local injection",
        "phases": reports,
        "transfer_cycles": transfer_cycles,
        "transfer_ns": transfer_cycles * 1e9 / timing.clock_hz,
        "total_cycles": total_cycles,
        "total_ns": total_cycles * 1e9 / timing.clock_hz,
    }
    report = json.dumps(result, indent=2)
    (root / "timing.json").write_text(report + "\n", encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
