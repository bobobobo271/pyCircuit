"""Execute independently specified programs on one unchanged machine binary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from machine_cases import cases
from machine_common import execute_program, verify_build


def check_case(case, result: dict) -> None:
    actual = Path(result["output"]).read_bytes()
    if actual != case.expected_bytes:
        raise AssertionError(f"{case.name}: actual node memory differs from literal byte oracle")
    observed = result["simulation"]["accounts"]
    for node, expected in case.expected_counts.items():
        samples = [entry for entry in observed if entry["node"] == node]
        if not samples or any(entry["counted"] != expected for entry in samples):
            raise AssertionError(f"{case.name}: node {node} receive count differs from {expected}")
    if case.expect_fault and result["simulation"]["destination_write_bytes"] != 0:
        raise AssertionError("range preflight failure wrote payload before rejecting")


# ndf: verifies=WSEMIG-MACHINE-VER-001
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    simulator = args.simulator.resolve()
    receipt = {"status": "INCOMPLETE", "build": verify_build(simulator), "runs": []}

    def save():
        (output / "verification.json").write_text(json.dumps(receipt, indent=2) + "\n")

    save()
    try:
        available = cases()
        selected = args.case or list(available)
        for name in selected:
            case = available[name]
            result = execute_program(
                simulator,
                case.program,
                case.patches,
                case.readback,
                output / name,
                expect_fault=case.expect_fault,
            )
            check_case(case, result)
            receipt["runs"].append({"case": name, "status": "PASS", **result})
            save()
            print(name + ": PASS", flush=True)
            if name == "tree-copy-duplicate":
                stalled = execute_program(
                    simulator,
                    case.program,
                    case.patches,
                    case.readback,
                    output / "tree-stalled",
                    stall={"pc": case.stall_copy_pc, "node": 33, "offset": 8, "cycles": 3},
                )
                check_case(case, stalled)
                if (
                    stalled["simulation"]["execution_cycles"]
                    != result["simulation"]["execution_cycles"] + 3
                ):
                    raise AssertionError(
                        "only a three-cycle permit withdrawal must add three cycles"
                    )
                snapshots = stalled["simulation"]["stall_observations"]
                if len(snapshots) != 3 or any(row != snapshots[0] for row in snapshots):
                    raise AssertionError("a stalled write changed PC, offset, stage or traffic")
                if (
                    stalled["inputs"] != result["inputs"]
                    or stalled["output_sha256"] != result["output_sha256"]
                ):
                    raise AssertionError(
                        "permit control changed the program, input or output bytes"
                    )
                receipt["runs"].append({"case": "tree-stalled", "status": "PASS", **stalled})
                save()
        if {entry["simulator_sha256"] for entry in receipt["runs"]} != {
            receipt["build"]["receipt"]["runner_sha256"]
        }:
            raise AssertionError("workloads did not execute the same compiled hardware")
        verify_build(simulator)
        receipt["status"] = "PASS"
    except Exception as error:
        receipt["error"] = str(error)
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
