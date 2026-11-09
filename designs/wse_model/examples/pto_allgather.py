"""Two rank-major AllGather kernels for the WSE Calendar functional profile.

Run with the separate PyPTO environment, not the WSE/pyCircuit environment.
The distributed runtime is not invoked: these are real compiler inputs.
"""

import argparse
import hashlib
import json
from pathlib import Path

import pypto.language as pl
import pypto.language.distributed as pld
from pypto import backend, codegen, ir
from pypto.backend import BackendType
from pypto.ir import OptimizationStrategy, PassManager


def allgather_kernel(ranks: int, words: int):
    @pl.program
    class AllGather:
        @pl.function(type=pl.FunctionType.InCore)
        def gather(
            self,
            source: pl.Tensor[[1, words], pl.FP32],
            target: pl.InOut[pld.DistributedTensor[[ranks, words], pl.FP32]],
            signal: pl.InOut[pld.DistributedTensor[[ranks, 1], pl.INT32]],
        ) -> pld.DistributedTensor[[ranks, words], pl.FP32]:
            return pld.tensor.allgather(source, target, signal)

    return AllGather


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    backend.set_backend_type(BackendType.Ascend910)
    phases = []
    for name, ranks, words, groups, source_base, target_base in (
        ("row", 8, 384, [list(range(r * 8, r * 8 + 8)) for r in range(4)], 0, 4096),
        ("column", 4, 3072, [list(range(c, 32, 8)) for c in range(8)], 4096, 16384),
    ):
        program = allgather_kernel(ranks, words)
        lowered = PassManager.get_strategy(OptimizationStrategy.Default).run_passes(program)
        text = codegen.PTOCodegen().generate(lowered, emit_tile_addr=True)
        (args.output / f"{name}.pto").write_text(text)
        (args.output / f"{name}.pypto-ir.py").write_text(ir.python_print(lowered))
        phases.append(
            {
                "name": name,
                "kernel": "gather",
                "ranks": ranks,
                "words": words,
                "dtype": "f32",
                "groups": groups,
                "source_base": source_base,
                "target_base": target_base,
                "fragments": 8,
                "input_shape": [1, words],
                "output_shape": [ranks, words],
                "layout": "rank-major-contiguous",
                "raw_pto_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    manifest = {
        "schema": "wse-pypto-allgather/1",
        "topology": [5, 8],
        "members": list(range(32)),
        "bank_bytes": 65536,
        "phases": phases,
        "dependency": {
            "producer": "row.target",
            "consumer": "column.source",
            "view": "contiguous-flatten",
            "bytes": 12288,
        },
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (args.output / "program.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
