"""Serial, alternating benchmark: one warmup and three measured runs per case.

Run only after all builds finish. All four executables use one worker and O1.
Payload verification applies to the detailed model; coarse checks traffic and
completion time. Trace output is disabled for every measured invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "inputs",
        "detailed-gfsim",
        "detailed-verilator",
        "coarse-gfsim",
        "coarse-verilator",
        "output",
    ):
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    inputs, output = args.inputs.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    executables = {
        "detailed-gfsim": args.detailed_gfsim.resolve(),
        "coarse-gfsim": args.coarse_gfsim.resolve(),
        "detailed-verilator": args.detailed_verilator.resolve(),
        "coarse-verilator": args.coarse_verilator.resolve(),
    }
    hashes = {name: digest(path) for name, path in executables.items()}
    detailed_build_path = executables["detailed-gfsim"].parent.parent / "build.json"
    coarse_build_path = executables["coarse-gfsim"].parent.parent / "build.json"
    detailed_build = json.loads(detailed_build_path.read_text())
    coarse_build = json.loads(coarse_build_path.read_text())
    if detailed_build["status"] != "PASS" or coarse_build["status"] != "PASS":
        raise AssertionError("both model builds must pass")
    if detailed_build["toolchain"] != coarse_build["toolchain"]:
        raise AssertionError("different SDK metadata")
    compiler_hashes = detailed_build["native_helpers"]
    compiler_hashes["pycircuit"] = detailed_build["compiler"]["sha256"]
    if any(
        coarse_build["sdk_binaries"].get(name) != value for name, value in compiler_hashes.items()
    ):
        raise AssertionError("models were built with different SDK binaries")
    for model, build in (("detailed-gfsim", detailed_build), ("coarse-gfsim", coarse_build)):
        if build["runner_sha256"] != hashes[model]:
            raise AssertionError("runner differs from recorded build")
    manifest = json.loads((inputs / "inputs.json").read_text())
    receipt = {
        "status": "INCOMPLETE",
        "conditions": {
            "warmups": 1,
            "samples": 3,
            "serial": True,
            "alternating_order": True,
            "optimization": "O1",
            "workers": 1,
            "trace": False,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "host_logical_cpus": os.cpu_count(),
        },
        "executables": {
            name: {"path": str(path), "sha256": hashes[name]} for name, path in executables.items()
        },
        "inputs_manifest": str(inputs / "inputs.json"),
        "inputs_manifest_sha256": digest(inputs / "inputs.json"),
        "matched_sdk_binaries": compiler_hashes,
        "build_receipts": {
            str(path): digest(path) for path in (detailed_build_path, coarse_build_path)
        },
        "runs": [],
    }

    def save() -> None:
        (output / "benchmark.json").write_text(json.dumps(receipt, indent=2) + "\n")

    save()
    try:
        for case in manifest["cases"]:
            name = case["name"]
            case_inputs = inputs / name
            expected_time = None
            for iteration in range(4):
                order = list(executables)
                if iteration % 2:
                    order.reverse()
                for model in order:
                    role = "warmup" if iteration == 0 else "sample"
                    folder = output / name / f"{role}-{iteration}-{model}"
                    folder.mkdir(parents=True)
                    simulator = executables[model]
                    if digest(simulator) != hashes[model]:
                        raise AssertionError("runner changed during benchmark")
                    simulation_path = folder / "simulation.json"
                    if model.startswith("detailed"):
                        command = [
                            simulator,
                            case_inputs / "program.bin",
                            case_inputs / "image.bin",
                            case_inputs / "readback.bin",
                            folder / "output.bin",
                            simulation_path,
                            "--config",
                            case_inputs / "timing.bin",
                            "--events",
                            "none",
                        ]
                    else:
                        command = [simulator, case_inputs / "coarse.bin", simulation_path]
                    row = {
                        "case": name,
                        "model": model,
                        "role": role,
                        "iteration": iteration,
                        "status": "RUNNING",
                        "command": list(map(str, command)),
                        "folder": str(folder),
                    }
                    receipt["runs"].append(row)
                    save()
                    logging.info("START %s %s-%s %s", name, role, iteration, model)
                    started = time.monotonic()
                    with (folder / "run.log").open("w") as log:
                        result = subprocess.run(
                            row["command"], stdout=log, stderr=subprocess.STDOUT, timeout=3600
                        )
                    row.update(
                        returncode=result.returncode, wall_seconds=time.monotonic() - started
                    )
                    result.check_returncode()
                    simulation = json.loads(simulation_path.read_text())
                    if simulation["status"] != "EXECUTED":
                        raise AssertionError("run did not execute successfully")
                    if model.startswith("detailed"):
                        if (folder / "output.bin").read_bytes() != (
                            case_inputs / "expected.bin"
                        ).read_bytes():
                            raise AssertionError(
                                "detailed payload differs from independent rank bytes"
                            )
                        for node, count in case["expected_counts"].items():
                            observed = [a for a in simulation["accounts"] if a["node"] == int(node)]
                            if not observed or any(a["counted"] != count for a in observed):
                                raise AssertionError("detailed receive count mismatch")
                        modeled_time = simulation["model_time_noc_cycles"]
                        reads, writes = (
                            simulation["source_read_bytes"],
                            simulation["destination_write_bytes"],
                        )
                        phases = {p["name"]: p["seconds"] for p in simulation["timing"]["phases"]}
                        row.update(
                            execution_seconds=phases["execution"],
                            initialization_seconds=phases["build_reset"],
                            load_seconds=sum(
                                value
                                for key, value in phases.items()
                                if key
                                in ("timing_config_load", "image_load", "program_load", "start")
                            ),
                            output_seconds=phases["readback_and_accounts"],
                        )
                    else:
                        modeled_time = simulation["model_time"]
                        reads, writes = simulation["read_bytes"], simulation["write_bytes"]
                        row.update(
                            execution_seconds=simulation["execution_seconds"],
                            initialization_seconds=simulation["initialize_seconds"],
                            load_seconds=simulation["load_seconds"],
                            output_seconds=None,
                        )
                    if expected_time is None:
                        expected_time = modeled_time
                    if modeled_time != expected_time:
                        raise AssertionError("models disagree on completion time")
                    if (reads, writes) != (
                        case["source_read_bytes"],
                        case["destination_write_bytes"],
                    ):
                        raise AssertionError("traffic differs from independent expected bytes")
                    if digest(simulator) != hashes[model] or any(
                        digest(case_inputs / filename) != expected
                        for filename, expected in case["inputs"].items()
                    ):
                        raise AssertionError("runner or shared inputs changed during execution")
                    row.update(status="PASS", simulation=simulation)
                    save()
                    logging.info(
                        f"PASS {name} {role}-{iteration} {model}: execution={row['execution_seconds']:.6f}s wall={row['wall_seconds']:.6f}s",
                    )
        summaries = []
        for case in manifest["cases"]:
            for model in executables:
                runs = [
                    r
                    for r in receipt["runs"]
                    if r["case"] == case["name"] and r["model"] == model and r["role"] == "sample"
                ]
                summary = {"case": case["name"], "model": model, "samples": len(runs)}
                for field in (
                    "execution_seconds",
                    "wall_seconds",
                    "initialization_seconds",
                    "load_seconds",
                    "output_seconds",
                ):
                    values = [r[field] for r in runs]
                    summary[field] = (
                        None
                        if values[0] is None
                        else {
                            "raw": values,
                            "median": statistics.median(values),
                            "min": min(values),
                            "max": max(values),
                        }
                    )
                summaries.append(summary)
        receipt.update(status="PASS", summary=summaries)
    except Exception as error:
        receipt.update(status="FAIL", error=str(error))
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
