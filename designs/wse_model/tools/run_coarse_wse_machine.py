"""Run a generated coarse backend from an address-free workload description."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from wse_model.programs.coarse import (  # noqa: E402
    Barrier,
    Profile,
    RetireGroup,
    Route,
    Transfer,
    WaitGroup,
    Workload,
)


def load_workload(path: Path) -> Workload:
    data = json.loads(path.read_text())
    profile_data = dict(data.get("profile", {}))
    profile_data["calendar"] = {
        (node, direction, slot): mask
        for node, direction, slot, mask in profile_data.get("calendar", [])
    }
    routes = [
        Route(item["id"], item["source"], tuple(map(tuple, item["edges"])), tuple(item["targets"]))
        for item in data["routes"]
    ]
    constructors = {cls.__name__: cls for cls in (Transfer, WaitGroup, Barrier, RetireGroup)}
    commands = []
    for item in data["commands"]:
        fields = dict(item)
        commands.append(constructors[fields.pop("kind")](**fields))
    return Workload(routes, commands, Profile(**profile_data))


def example(name: str) -> Workload:
    if name == "allgather32":
        from wse_model.programs.coarse_allgather import allgather32

        return allgather32(Profile())
    if name == "single-hop":
        edges, targets = ((0, 1),), (1,)
    elif name == "two-hop":
        edges, targets = ((0, 1), (1, 2)), (2,)
    else:
        edges = ((0, 1), (1, 2), (1, 9), (9, 17), (17, 25), (25, 33))
        targets = (0, 2, 33)
    return Workload((Route(0, 0, edges, targets),), (Transfer(0, 0, 0, 200),), Profile.open())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--workload", type=Path)
    source.add_argument("--case", choices=("single-hop", "two-hop", "multicast", "allgather32"))
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    workload = load_workload(args.workload) if args.workload else example(args.case)
    loader = workload.write(output)
    command = [str(args.runner.resolve()), str(loader), str(output / "receipt.json")]
    if args.trace:
        command.append(str(output / "events.jsonl"))
    result = subprocess.run(command, check=False)
    (output / "command.json").write_text(
        json.dumps({"argv": command, "returncode": result.returncode}, indent=2) + "\n"
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
