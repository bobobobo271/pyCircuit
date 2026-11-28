"""Build an unchanged baseline using the explicitly selected comparison SDK.

The baseline's historical SDK pin is retained. This adapter replaces only its
pin rejection and records both the original pin and actual SDK in build.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--fixed-ledger", action="store_true")
    parser.add_argument("--trace-toggle", action="store_true")
    args, forwarded = parser.parse_known_args()
    root = args.baseline.resolve() / "designs/wse_model"
    script = root / "tools/build_wse_machine.py"
    original = script.read_text()
    guard = '        raise ValueError("installed compiler/runtime must match the consumer dependency pin")'
    if original.count(guard) != 1:
        raise ValueError("baseline pin guard differs from reviewed adapter contract")
    adapted = original.replace(guard, "        pass  # Explicit comparison SDK override.")
    if args.fixed_ledger:
        adapted = adapted.replace(
            'if args.model == "timed":\n        sources.append(Path(__file__).with_name("fixed_timed_ledger.py"))',
            'if True:\n        sources.append(Path(__file__).with_name("fixed_timed_ledger.py"))',
        ).replace(
            'if stem in ("ledger", "timed_machine"):',
            'if stem in ("ledger", "timed_machine", "machine"):',
        )
    output = Path(forwarded[forwarded.index("--output") + 1]).resolve()
    model = forwarded[forwarded.index("--model") + 1] if "--model" in forwarded else "functional"
    pin = root / (
        "models/machine/timed-pycircuit.lock.json"
        if model == "timed"
        else "models/calendar/pycircuit.lock.json"
    )
    os.environ["PYC_TOOLCHAIN_ROOT"] = str(args.toolchain.resolve())
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    sys.path.insert(0, str(root / "tools"))
    sys.argv = [str(script), *forwarded]
    namespace = {"__file__": str(script), "__name__": "comparison_baseline_builder"}
    if args.trace_toggle:
        marker = "    top = (\n"
        adapted = adapted.replace(
            marker, "    driver = _comparison_driver(driver, output)\n" + marker
        )

        def comparison_driver(driver: Path, directory: Path) -> Path:
            original_driver = driver.read_text()
            changed = original_driver.replace(
                '    simulation.trace.open(event_path);\n    require(bool(simulation.trace), "cannot create event trace");',
                '    if (event_path != "none") {\n      simulation.trace.open(event_path);\n'
                '      require(bool(simulation.trace), "cannot create event trace");\n    }',
            )
            if changed == original_driver:
                raise ValueError("trace-toggle requires the reviewed timed driver")
            destination = directory / "comparison_driver.cpp"
            destination.write_text(changed)
            return destination

        namespace["_comparison_driver"] = comparison_driver
    exec(compile(adapted, str(script), "exec"), namespace)
    try:
        namespace["main"]()
    finally:
        receipt_path = output / "build.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            receipt["comparison_sdk_override"] = {
                "original_pin": json.loads(pin.read_text()),
                "actual_sdk": str(args.toolchain.resolve()),
                "adapter": str(Path(__file__).resolve()),
                "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "python": sys.executable,
                "fixed_ledger_specialization": args.fixed_ledger,
                "trace_toggle_adapter": args.trace_toggle,
                "transformation": (
                    "Replace SDK pin mismatch rejection; optionally apply the existing fixed_timed_ledger "
                    "materializer to functional ledger and machine. Original source files unchanged."
                ),
            }
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
