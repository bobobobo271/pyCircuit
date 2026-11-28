"""One measured baseline run with independently checked payload and traffic."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--simulator", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=("two-hop", "allgather32"), required=True)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--functional", action="store_true")
    args = parser.parse_args()
    root = args.baseline.resolve() / "designs/wse_model"
    sys.path[:0] = [str(root / "src"), str(root / "tools")]
    from fixtures import allgather32
    from machine_common import digest
    from timed_machine_cases import open_calendar, two_hop_pipeline
    from verify_wse_machine import check_case

    from wse_model.programs.abi import Kind, Program, encode_image, encode_readback
    from wse_model.programs.timing import encode_config, load_timing_profile

    case = (
        allgather32(timed=not args.functional) if args.case == "allgather32" else two_hop_pipeline()
    )
    program = case.program
    if args.functional and args.case == "two-hop":
        from dataclasses import replace

        program = Program(
            tuple(
                replace(command, kind=Kind.COPY) if command.kind == Kind.SEND else command
                for command in program.commands
                if command.kind != Kind.BARRIER
            )
        )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "program.bin").write_bytes(program.pack())
    (output / "image.bin").write_bytes(encode_image(case.patches, program.profile))
    (output / "readback.bin").write_bytes(encode_readback(case.readback, program.profile))
    (output / "expected.bin").write_bytes(case.expected_bytes)
    command = [
        args.simulator.resolve(),
        output / "program.bin",
        output / "image.bin",
        output / "readback.bin",
        output / "output.bin",
        output / "simulation.json",
    ]
    if not args.functional:
        profile = load_timing_profile(root / "models/machine/timing_profile.json")
        calendar = open_calendar() if args.case == "two-hop" else None
        (output / "timing.bin").write_bytes(encode_config(profile, calendar=calendar))
        command += [
            "--config",
            output / "timing.bin",
            "--events",
            output / "events.jsonl" if args.trace else "none",
        ]
    inputs = {path.name: digest(path) for path in output.glob("*.bin")}
    receipt = {
        "status": "INCOMPLETE",
        "command": list(map(str, command)),
        "inputs": inputs,
        "runner_sha256": digest(args.simulator),
        "trace": args.trace,
        "case": case.name,
        "timing_scope": "one subprocess; driver separately records load, execution and readback",
    }
    started = time.monotonic()
    try:
        with (output / "run.log").open("w") as log:
            result = subprocess.run(
                receipt["command"], stdout=log, stderr=subprocess.STDOUT, timeout=3600
            )
        receipt.update(returncode=result.returncode, wall_seconds=time.monotonic() - started)
        result.check_returncode()
        simulation = json.loads((output / "simulation.json").read_text())
        if simulation["status"] != "EXECUTED":
            raise AssertionError("baseline did not complete successfully")
        check_case(case, {"output": str(output / "output.bin"), "simulation": simulation})
        if {name: digest(output / name) for name in inputs} != inputs:
            raise AssertionError("inputs changed during execution")
        receipt.update(
            status="PASS", simulation=simulation, output_sha256=digest(output / "output.bin")
        )
    except Exception as error:
        receipt.update(status="FAIL", error=str(error))
        raise
    finally:
        (output / "execution.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
