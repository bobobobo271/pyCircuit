"""Build the WSE payload consumer through public compile/link/emit commands.

Source the pinned pyCircuit environment before running this script. Every stage
retains its command, exit status, elapsed time and unmodified output. Workloads
are runtime inputs and are not embedded in generated Python or intermediate IR.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--emit-verilog", action="store_true")
    parser.add_argument("--optimization", choices=("O0", "O1"), default="O1")
    parser.add_argument("--timeout", type=int, default=10800)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    toolchain = Path(os.environ["PYC_TOOLCHAIN_ROOT"])
    source = ROOT / "models/pto_allgather"
    compiler = str(toolchain / "bin/pycircuit")
    receipt = {"status": "INCOMPLETE", "stages": [], "sources": {}}
    receipt["compiler"] = {
        "path": compiler,
        "sha256": hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
    }
    receipt["build_script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    receipt["conditions"] = {
        "optimization": args.optimization,
        "build_parallelism": 1,
        "simulation_workers": 1,
        "cc": os.environ.get("CC"),
        "cxx": os.environ.get("CXX"),
    }
    receipt["toolchain"] = json.loads(
        (toolchain / "share/pycircuit/toolchain-metadata.json").read_text()
    )
    receipt["native_helpers"] = {
        name: hashlib.sha256((toolchain / "bin" / name).read_bytes()).hexdigest()
        for name in ("pycircuit-source-unit", "pycircuit-link", "pycircuit-emit")
    }
    receipt_path = output / "build.json"

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

    def run(name, command):
        entry = {"name": name, "command": [str(x) for x in command]}
        receipt["stages"].append(entry)
        save()
        started = time.monotonic()
        with (output / f"{name}.log").open("w") as log:
            try:
                result = subprocess.run(
                    entry["command"],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=args.timeout,
                    check=False,
                )
                entry["returncode"] = result.returncode
            except subprocess.TimeoutExpired:
                entry["returncode"] = None
                entry["timeout"] = args.timeout
        entry["seconds"] = time.monotonic() - started
        save()
        print(f"{name}: {entry['returncode']} ({entry['seconds']:.2f}s)", flush=True)
        if entry["returncode"] != 0:
            raise RuntimeError(f"{name} failed; see {output / (name + '.log')}")

    try:
        units = []
        sources = [] if args.probe else [(ROOT / "models/calendar", "forward", "wse_calendar")]
        name = "memory_probe" if args.probe else "payload"
        sources.append((source, name, "wse_payload"))
        (output / "units").mkdir(exist_ok=True)
        for directory, stem, package in sources:
            path = directory / f"{stem}.py"
            receipt["sources"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            unit = output / "units" / stem
            imports = [item for prior in units for item in ("-I", prior)]
            run(
                f"compile-{stem}",
                [
                    compiler,
                    "compile",
                    "-c",
                    path,
                    "--source-root",
                    directory,
                    "--package-prefix",
                    package,
                    *imports,
                    "-o",
                    unit,
                    "--replace",
                ],
            )
            units.append(unit)
        top = "MemoryProbe" if args.probe else "PayloadAllGather"
        final = output / "final.ac"
        run(
            "link",
            [
                compiler,
                "link",
                *units,
                "--top",
                f"wse_payload.{name}.{top}",
                "-o",
                final,
                "--replace",
            ],
        )
        receipt["final_sha256"] = hashlib.sha256(final.read_bytes()).hexdigest()
        run(
            "emit-cpp",
            [
                compiler,
                "emit",
                final,
                "--target",
                "cpp",
                "-o",
                output / "cpp",
                "--replace",
            ],
        )
        if args.emit_verilog:
            run(
                "emit-verilog",
                [
                    compiler,
                    "emit",
                    final,
                    "--target",
                    "verilog",
                    "-o",
                    output / "verilog",
                    "--replace",
                ],
            )
        consumer = output / "consumer"
        consumer.mkdir(exist_ok=True)
        driver = source / ("memory_probe_driver.cpp" if args.probe else "driver.cpp")
        receipt["sources"][str(driver)] = hashlib.sha256(driver.read_bytes()).hexdigest()
        (consumer / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.25)\nproject(WsePayload LANGUAGES C CXX)\n"
            f'add_subdirectory("{output / "cpp"}" generated)\n'
            f'add_executable(payload_runner "{driver}")\n'
            "target_compile_features(payload_runner PRIVATE cxx_std_20)\n"
            "target_link_libraries(payload_runner PRIVATE pycircuit_modules)\n"
        )
        run(
            "configure",
            [
                "cmake",
                "-S",
                consumer,
                "-B",
                output / "build",
                "-G",
                "Ninja",
                f"-DCMAKE_PREFIX_PATH={toolchain}",
                "-DCMAKE_BUILD_TYPE=Release",
                f"-DCMAKE_CXX_FLAGS_RELEASE=-{args.optimization} -DNDEBUG",
            ],
        )
        run("build", ["cmake", "--build", output / "build", "--parallel", "1"])
        if args.probe:
            run("probe-run", [output / "build/payload_runner"])
        receipt["status"] = "PASS"
        receipt["runner"] = str(output / "build/payload_runner")
        receipt["runner_sha256"] = hashlib.sha256(
            (output / "build/payload_runner").read_bytes()
        ).hexdigest()
    except Exception as error:
        receipt["status"] = "FAIL"
        receipt["error"] = str(error)
        save()
        raise
    save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
