"""Materialize identical communication inputs for both comparison models."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current = Path(__file__).resolve().parents[2]
    baseline = args.baseline.resolve() / "designs/wse_model"
    sys.path[:0] = [str(current / "src"), str(baseline / "tools")]
    import wse_model
    import wse_model.programs

    # Legacy input interpretation is explicitly isolated in verification.
    wse_model.__path__.append(str(baseline / "src/wse_model"))
    wse_model.programs.__path__.append(str(baseline / "src/wse_model/programs"))
    from adapter import from_legacy, profile_from_legacy
    from fixtures import comparison_cases
    from timed_machine_cases import open_calendar

    from wse_model.programs.abi import encode_image, encode_readback
    from wse_model.programs.coarse_allgather import allgather32
    from wse_model.programs.timing import encode_config, load_timing_profile

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    timing = load_timing_profile(baseline / "models/machine/timing_profile.json")
    receipt = {"status": "INCOMPLETE", "baseline": str(baseline), "cases": []}
    for case in comparison_cases():
        calendar = open_calendar() if case.name == "timed-two-hop-pipeline" else None
        profile = profile_from_legacy(timing, calendar=calendar)
        coarse = from_legacy(case.program, profile)
        if case.name == "allgather32-128B":
            direct = allgather32(profile)
            if direct.records() != coarse.records():
                raise AssertionError("address-free AllGather differs from legacy communication")
        folder = output / case.name
        folder.mkdir()
        coarse.write(folder / "coarse.bin")
        for name, data in (
            ("program.bin", case.program.pack()),
            ("image.bin", encode_image(case.patches, case.program.profile)),
            ("readback.bin", encode_readback(case.readback, case.program.profile)),
            ("expected.bin", case.expected_bytes),
            ("timing.bin", encode_config(timing, calendar=calendar)),
        ):
            (folder / name).write_bytes(data)
        receipt["cases"].append(
            {
                "name": case.name,
                "source_read_bytes": case.expected_traffic[0],
                "destination_write_bytes": case.expected_traffic[1],
                "expected_bytes": len(case.expected_bytes),
                "expected_counts": case.expected_counts,
                "inputs": {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in folder.glob("*.bin")
                },
                "calendar": "all-open" if calendar is not None else "fixed-source-slots",
                "direct_coarse_loader_equals_legacy_adapter": case.name == "allgather32-128B",
            }
        )
    receipt["status"] = "PASS"
    (output / "inputs.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
