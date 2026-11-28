"""Build the metadata-only WSE model with the explicitly selected SDK."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--optimization", choices=["O0", "O1", "O2"], default="O1")
    args = p.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    sdk = Path(os.environ["PYC_TOOLCHAIN_ROOT"]).resolve()
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    for key, name in [
        ("PYCIRCUIT_SOURCE_COMPILER", "pycircuit-source-unit"),
        ("PYCIRCUIT_LINKER", "pycircuit-link"),
        ("PYCIRCUIT_EMITTER", "pycircuit-emit"),
    ]:
        env[key] = str(sdk / "bin" / name)
    source = ROOT / "models/coarse"
    receipt = {
        "status": "INCOMPLETE",
        "toolchain": json.loads((sdk / "share/pycircuit/toolchain-metadata.json").read_text()),
        "optimization": args.optimization,
        "workers": 1,
        "stages": [],
        "sources": {
            str(f): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in source.iterdir()
            if f.is_file()
        },
    }

    receipt["sdk_binaries"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (sdk / "bin").glob("pycircuit*")
        if p.is_file()
    }
    receipt["builder_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def save():
        (out / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")

    def run(name, command):
        started = time.monotonic()
        with (out / (name + ".log")).open("w") as log:
            r = subprocess.run(
                list(map(str, command)), env=env, stdout=log, stderr=subprocess.STDOUT
            )
        receipt["stages"].append(
            {
                "name": name,
                "command": list(map(str, command)),
                "seconds": time.monotonic() - started,
                "returncode": r.returncode,
            }
        )
        save()
        print(name, r.returncode, flush=True)
        if r.returncode:
            raise RuntimeError(str(out / (name + ".log")))

    try:
        units = []
        (out / "units").mkdir()
        for stem in ("types", "event_queue", "machine"):
            unit = out / "units" / stem
            run(
                "compile-" + stem,
                [
                    sdk / "bin/pycircuit",
                    "compile",
                    "-c",
                    source / (stem + ".py"),
                    "--source-root",
                    source,
                    "--package-prefix",
                    "wse_coarse",
                    *[v for u in units for v in ("-I", u)],
                    "-o",
                    unit,
                ],
            )
            units.append(unit)
        run(
            "link",
            [
                sdk / "bin/pycircuit",
                "link",
                *units,
                "--top",
                "wse_coarse.machine.CoarseWseMachine",
                "-o",
                out / "final.ac",
            ],
        )
        for target in ("cpp", "verilog"):
            run(
                "emit-" + target,
                [
                    sdk / "bin/pycircuit",
                    "emit",
                    out / "final.ac",
                    "--target",
                    target,
                    "-o",
                    out / target,
                ],
            )
        consumer = out / "consumer"
        consumer.mkdir()
        (consumer / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.25)\nproject(Coarse LANGUAGES C CXX)\n"
            + f'add_subdirectory("{out / "cpp"}" generated)\nadd_executable(coarse_runner "{source / "driver.cpp"}")\ntarget_compile_features(coarse_runner PRIVATE cxx_std_20)\ntarget_link_libraries(coarse_runner PRIVATE pycircuit_modules)\n'
        )
        run(
            "configure",
            [
                "cmake",
                "-S",
                consumer,
                "-B",
                out / "build",
                "-G",
                "Ninja",
                f"-DCMAKE_PREFIX_PATH={sdk}",
                "-DCMAKE_BUILD_TYPE=Release",
                f"-DCMAKE_CXX_FLAGS_RELEASE=-{args.optimization} -DNDEBUG",
            ],
        )
        run("build", ["cmake", "--build", out / "build", "--parallel", "2"])
        receipt["runner"] = str(out / "build/coarse_runner")
        receipt["final_sha256"] = hashlib.sha256((out / "final.ac").read_bytes()).hexdigest()
        receipt["runner_sha256"] = hashlib.sha256(
            (out / "build/coarse_runner").read_bytes()
        ).hexdigest()
        if any(
            hashlib.sha256(Path(p).read_bytes()).hexdigest() != h
            for p, h in receipt["sources"].items()
        ):
            raise RuntimeError("build source changed during compilation")
        receipt["status"] = "PASS"
    except Exception as e:
        receipt.update(status="FAIL", error=str(e))
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
