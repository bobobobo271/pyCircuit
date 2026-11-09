"""Run the actual PyPTO -> PTOAS -> WSE generated simulator chain.

Build the simulator once with build_pto_allgather.py. This runner changes data
and permit stimuli without rebuilding the pyCircuit hardware model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command, directory, label, env=None, *, reject=False):
    started = time.monotonic()
    with (directory / f"{label}.log").open("w") as log:
        process = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    result = {
        "command": [str(value) for value in command],
        "exit_code": process.returncode,
        "wall_seconds": time.monotonic() - started,
        "log": str(directory / f"{label}.log"),
    }
    (directory / f"{label}.stage.json").write_text(json.dumps(result, indent=2) + "\n")
    if reject:
        if process.returncode == 0:
            raise RuntimeError(f"{label}: expected malformed workload rejection")
    elif process.returncode:
        raise RuntimeError(f"{label} failed; see {directory / (label + '.log')}")
    return result


def input_bytes(seed):
    # Exact finite FP32 integers, with node and element both distinguishable.
    return b"".join(
        struct.pack("<f", float(seed * 400000 + node * 10000 + element))
        for node in range(32)
        for element in range(384)
    )


def verify_build(simulator):
    path = simulator.parent.parent / "build.json"
    build = json.loads(path.read_text())
    if build["status"] != "PASS" or build["runner_sha256"] != digest(simulator):
        raise ValueError("simulator does not match a successful recorded build")
    for source, expected in build["sources"].items():
        if digest(Path(source)) != expected:
            raise ValueError(f"model source changed since build: {source}")
    if digest(path.parent / "final.ac") != build["final_sha256"]:
        raise ValueError("final ACIR changed since build")
    return {"path": str(path), "sha256": digest(path), "receipt": build}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pypto-python", type=Path, required=True)
    parser.add_argument("--ptoas-python", type=Path, required=True)
    parser.add_argument("--ptoas-root", type=Path, required=True)
    parser.add_argument("--simulator", type=Path, required=True)
    parser.add_argument("--timing-simulator", type=Path)
    parser.add_argument(
        "--timing-profile", type=Path, default=ROOT / "models/pto_allgather/timing_profile.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    compiler = output / "compiler"
    compiler.mkdir()
    receipt = {"status": "INCOMPLETE", "stages": {}, "runs": []}
    try:
        simulator_path = args.simulator.resolve()
        receipt["build"] = verify_build(simulator_path)
        receipt["conditions"] = {
            "backend": "cpp",
            "execution": "serialized-functional",
            "carrier_nodes": 40,
            "participants": 32,
            "bank_bytes_per_participant": 65536,
            "payload_beat_bytes": 8,
            "simulation_workers": 1,
            "cycle_boundary": "after start, before output readback",
            "byte_metric": "logical payload bytes counted from successful DUT stages",
        }
        env = dict(os.environ, PYPTO_SKIP_RUNTIME_PIN_CHECK="1")
        receipt["stages"]["pypto"] = run(
            [
                str(args.pypto_python.absolute()),
                str(ROOT / "examples/pto_allgather.py"),
                "--output",
                str(compiler),
            ],
            output,
            "pypto",
            env,
        )
        pto_python = str(args.ptoas_python.absolute())
        pto_root = args.ptoas_root.resolve()
        for phase in ("row", "column"):
            receipt["stages"][f"ptoas-{phase}"] = run(
                [
                    pto_python,
                    str(pto_root / "bin/ptoas"),
                    str(compiler / f"{phase}.pto"),
                    "--pto-arch=a3",
                    "--pto-level=level3",
                    "--enable-insert-sync",
                    "--emit-pto-ir",
                    "-o",
                    str(compiler / f"{phase}.lowered.pto"),
                ],
                output,
                f"ptoas-{phase}",
            )
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(pto_root), str(ROOT / "src"))))
        for tool in ("lower_pto_allgather", "verify_pto_allgather_ir"):
            receipt["stages"][tool] = run(
                [pto_python, str(ROOT / f"tools/{tool}.py"), "--compiler-dir", str(compiler)],
                output,
                tool,
                env,
            )
        receipt["stages"]["analytical-timing"] = run(
            [
                pto_python,
                str(ROOT / "tools/analyze_pto_allgather_timing.py"),
                "--compiler-dir",
                str(compiler),
                "--output",
                str(output / "timing-analysis.json"),
                "--profile",
                str(args.timing_profile.resolve()),
            ],
            output,
            "analytical-timing",
            env,
        )
        receipt["timing_status"] = json.loads((output / "timing-analysis.json").read_text())[
            "status"
        ]
        receipt["timing_backend_status"] = "NOT_RUN"
        if args.timing_simulator:
            receipt["stages"]["generated-timing"] = run(
                [
                    pto_python,
                    str(ROOT / "tools/verify_pto_allgather_timing.py"),
                    "--compiler-dir",
                    str(compiler),
                    "--profile",
                    str(args.timing_profile.resolve()),
                    "--simulator",
                    str(args.timing_simulator.resolve()),
                    "--output",
                    str(output / "timing-execution"),
                ],
                output,
                "generated-timing",
                env,
            )
            receipt["timing_backend_status"] = json.loads(
                (output / "timing-execution/verification.json").read_text()
            )["status"]
        simulator = str(simulator_path)
        receipt["simulator_sha256"] = digest(simulator_path)
        for label, seed, stall in (
            ("baseline", 1, False),
            ("new-data", 7, False),
            ("stall", 1, True),
        ):
            directory = output / label
            directory.mkdir()
            data = input_bytes(seed)
            (directory / "input.bin").write_bytes(data)
            command = [
                simulator,
                str(directory / "input.bin"),
                str(compiler / "commands.bin"),
                str(directory / "output.bin"),
                str(directory / "simulation.json"),
            ]
            if stall:
                command += ["--stall-descriptor", "260", "--stall-node", "8", "--stall-cycles", "3"]
            stage = run(command, directory, "simulation")
            actual = (directory / "output.bin").read_bytes()
            if actual != data * 32:
                raise AssertionError(
                    f"{label}: all-rank output does not match original input concatenation"
                )
            simulation = json.loads((directory / "simulation.json").read_text())
            if (simulation["source_read_bytes"], simulation["destination_write_bytes"]) != (
                442368,
                1966080,
            ):
                raise AssertionError(
                    f"{label}: logical payload traffic differs from two AllGathers"
                )
            receipt["runs"].append(
                {
                    "case": label,
                    "status": "PASS",
                    "seed": seed,
                    "checked_bytes": len(actual),
                    "stage": stage,
                    "input_sha256": digest(directory / "input.bin"),
                    "output_sha256": digest(directory / "output.bin"),
                    "simulation": simulation,
                }
            )
        baseline, new_data, stalled = receipt["runs"]
        cycles = [r["simulation"]["execution_cycles"] for r in receipt["runs"]]
        if cycles[1] != cycles[0] or cycles[2] != cycles[0] + 3:
            raise AssertionError(
                "data-only or three-cycle permit control changed unexpected cycles"
            )
        if (
            baseline["input_sha256"] != stalled["input_sha256"]
            or baseline["output_sha256"] != stalled["output_sha256"]
            or baseline["output_sha256"] == new_data["output_sha256"]
        ):
            raise AssertionError("input/output control-variable hashes disagree")
        receipt["control_comparison"] = {
            "status": "PASS",
            "new_data_cycle_delta": cycles[1] - cycles[0],
            "stall_cycle_delta": cycles[2] - cycles[0],
        }
        bad = output / "out-of-order"
        bad.mkdir()
        commands = (compiler / "commands.bin").read_bytes()
        (bad / "commands.bin").write_bytes(commands[48:96] + commands[:48] + commands[96:])
        receipt["stages"]["order-rejection"] = run(
            [
                simulator,
                str(output / "baseline/input.bin"),
                str(bad / "commands.bin"),
                str(bad / "output.bin"),
                str(bad / "simulation.json"),
            ],
            bad,
            "simulation",
            reject=True,
        )
        if (bad / "output.bin").exists():
            raise AssertionError("failed workload published a completed output")
        if "descriptor fragment order" not in (bad / "simulation.log").read_text():
            raise AssertionError(
                "negative case did not fail on its intended descriptor-order check"
            )
        receipt["order_rejection"] = "PASS"
        receipt["status"] = "PASS"
    except Exception as error:
        receipt["error"] = str(error)
        raise
    finally:
        receipt["sources"] = {
            str(path.relative_to(ROOT)): digest(path)
            for path in (
                ROOT / "examples/pto_allgather.py",
                ROOT / "tools/lower_pto_allgather.py",
                ROOT / "src/wse_model/compiler/allgather.py",
                ROOT / "tools/verify_pto_allgather_ir.py",
                ROOT / "tools/analyze_pto_allgather_timing.py",
                ROOT / "src/wse_model/analysis/allgather_timing.py",
                ROOT / "src/wse_model/analysis/allgather_schedule.py",
                ROOT / "tools/verify_pto_allgather_timing.py",
                Path(__file__).resolve(),
            )
        }
        (output / "verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(output / "verification.json")


if __name__ == "__main__":
    main()
