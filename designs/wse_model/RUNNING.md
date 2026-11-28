# 构建与运行

使用 Python 3.11+、C++20 编译器、CMake、Ninja，以及明确选择的 pyCircuit SDK。
`PYC_TOOLCHAIN_ROOT` 指向该 SDK。Verilator 作为第二个仿真后端需单独安装。

从仓库根目录构建两种后端：

```sh
python3 designs/wse_model/tools/build_coarse_machine.py --output /absolute/new/build
python3 designs/wse_model/tools/build_coarse_rtl.py --build /absolute/new/build
```

第一条命令依次编译 `types.py`、`event_queue.py`、`machine.py`，链接统一的
`final.ac`，生成 C++ 与 Verilog，再构建 GFSIM runner。第二条使用同一份 Verilog
构建 Verilator runner；生成结果、日志与回执保留在指定目录。

准备一个运行时输入：

```python
from pathlib import Path
from wse_model.programs.coarse import Profile, Route, Transfer, Workload

workload = Workload(
    (Route(0, 0, ((0, 1), (1, 2)), (2,)),),
    (Transfer(1, 0, 0, 200),),
    Profile.open(),
)
workload.write(Path('/absolute/new/case'))
```

设置 `PYTHONPATH=designs/wse_model/src` 后执行上述脚本。两后端接收同一个
`loader.bin`，调用形式均为：

```text
runner loader.bin receipt.json [events.jsonl]
```

不提供事件文件时关闭轨迹输出，适合测执行速度。提供事件文件用于正确性检查。
回执分别记录初始化、装载和执行秒数，以及模型周期、宿主执行步数和字节统计。

独立验收使用 pytest，按实际构建位置设置：

```sh
export WSE_COARSE_SIMULATORS='{"gfsim":"/absolute/new/build/build/coarse_runner","verilator":"/absolute/new/build/verilator/obj/coarse_verilator"}'
export WSE_COARSE_TEST_OUTPUT=/absolute/new/verification
python3 -m pytest designs/wse_model/tests/test_coarse.py -v
```

原模型对照工具位于 `verification/coarse_compare/`。它读取独立旧分支工作区，
记录原始程序、数据、预期输出与工具链身份。原始失败和临时提高编译预算的重试
分别保存。构建成功、功能验证通过和性能测量是三个独立状态。
