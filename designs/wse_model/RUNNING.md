# 构建与运行

以下命令从 `designs/wse_model` 执行，输出放在新建的 `.wse_model_out/` 目录中。
本项目不附带已有构建产物、日志或测试结果。运行时生成的 JSON 回执和日志由
脚本保存在指定输出目录，可自行检查。

## 依赖

| 用途 | 依赖 |
| --- | --- |
| 普通 Python 语义及测试 | Python 3.11+，`pip install -e '.[dev]'` |
| pyCircuit 原生模型 | `models/calendar/pycircuit.lock.json` 指定的集成 checkout 与其 SDK，LLVM/MLIR 22.1.8，CMake、Ninja、C++20 编译器 |
| Calendar Verilog | 另需 Icarus Verilog 和 VVP |
| PTO 输入生成 | PyPTO `9339b96dd6d42e69b1dee8f43316d407887e2d43` 及其独立 Python 环境 |
| PTO IR 处理 | PTOAS v0.72，及与其 MLIR Python 扩展匹配的 Python 环境 |

pyCircuit 集成提交目前尚未发布。它包含参数循环、分析预算调整和大 Work
拆分等本地修改，不能用本仓 `main` 编译器或单独一个上游补丁替代。
以下原生命令的前提是已取得该集成 checkout，并从同一版本构建、安装 SDK。
仅安装本项目的 Python 包不会安装这些外部工具。

为当前 shell 设置实际路径：

```sh
export PYCIRCUIT_ROOT=/absolute/path/to/integrated-pycircuit
export PYC_TOOLCHAIN_ROOT=/absolute/path/to/installed-sdk
export PATH="$PYC_TOOLCHAIN_ROOT/bin:$PATH"
export PYTHONPATH="$PWD/src:$PYCIRCUIT_ROOT/python${PYTHONPATH:+:$PYTHONPATH}"
```

同时按本机编译器环境设置 `CC`、`CXX` 和系统 SDK 路径。
源端和工具链都应与依赖锁一致。

## 普通 Python

```sh
make check
make integration
python -m wse_model --json run ffn-allgather
python -m wse_model --json report matmul --m 1 --k 4096 --n 4096 --precision fp16
```

集成测试中的外部载荷比较需要设置实际输入、输出文件路径；未提供时该项跳过。
PyPTO 示例检查需要 `AG_PYPTO_PY` 指向独立解释器，未提供时单独跳过该示例。
提供该变量时，集成测试会执行 PyPTO 输入编译并写入临时目录。
普通 Python 测试不编译或运行 pyCircuit 电路。

## Calendar 电路

先运行较小的接收模块，两种后端都执行：

```sh
python tools/verify_calendar.py \
  --pycircuit-root "$PYCIRCUIT_ROOT" \
  --toolchain "$PYC_TOOLCHAIN_ROOT" \
  --suite receive --target both \
  --output "$PWD/.wse_model_out/receive"
```

完整闭环的入口为 `allgather-control` 和 `allgather-delayed`：

```sh
ulimit -s 65520
python tools/verify_calendar.py \
  --pycircuit-root "$PYCIRCUIT_ROOT" \
  --toolchain "$PYC_TOOLCHAIN_ROOT" \
  --suite allgather-control --target cpp --cpp-opt-level 0 \
  --timeout 21600 --stack-kib 65520 \
  --output "$PWD/.wse_model_out/allgather-control"
```

`--stack-kib` 检查继承栈上限，不负责修改；上面的 `ulimit` 受系统 hard limit
约束，应先确认系统允许该值。

完整闭环的源码链接与构建开销较大。生成 RTL 的 CMake 入口调用 SDK helper
时仍使用其 180 秒默认限制；大模型可在保留同一生成 RTL 的情况下，直接调用
现有 helper 的较长时限。把 `RTL_PROJECT` 设为生成的 Verilog 目录：

```sh
python "$PYC_TOOLCHAIN_ROOT/share/pycircuit/cmake/verify_example.py" \
  --compile-system "$RTL_PROJECT" \
  --output "$PWD/.wse_model_out/rtl/pycircuit_sim" \
  --include "$PYC_TOOLCHAIN_ROOT/include" \
  --iverilog "$(command -v iverilog)" --vvp "$(command -v vvp)" --timeout 21600
```

该命令只构建仿真程序。完整功能比较仍由 `tools/verify_calendar.py` 及
`models/calendar/host_checks.py` 中的检查负责。

## PTO 实际载荷

构建一次载荷模拟器，默认生成 C++，编译优化为 O1、单编译任务：

```sh
python tools/build_pto_allgather.py \
  --output "$PWD/.wse_model_out/payload-build"
```

PyPTO 与 PTOAS 分别使用匹配的解释器，不要把它们混装到本项目环境中。
设置 `AG_PYPTO_PY`、`AG_PTOAS_PY` 和 `AG_PTOAS_ROOT` 为各自的实际路径后执行：

```sh
python tools/run_pto_allgather.py \
  --pypto-python "$AG_PYPTO_PY" \
  --ptoas-python "$AG_PTOAS_PY" \
  --ptoas-root "$AG_PTOAS_ROOT" \
  --simulator "$PWD/.wse_model_out/payload-build/build/payload_runner" \
  --output "$PWD/.wse_model_out/payload-run"
```

运行器重新生成并处理 PTO IR，生成运行时描述符，执行不同输入和许可条件，
逐字节检查所有节点的真实输出。工作负载在运行时输入，不嵌入生成的电路源码。

## 暂定时序模型

先构建独立时序模拟器，再将它交给同一整链入口：

```sh
python tools/build_pto_allgather_timing.py \
  --output "$PWD/.wse_model_out/timing-build"
python tools/run_pto_allgather.py \
  --pypto-python "$AG_PYPTO_PY" \
  --ptoas-python "$AG_PTOAS_PY" \
  --ptoas-root "$AG_PTOAS_ROOT" \
  --simulator "$PWD/.wse_model_out/payload-build/build/payload_runner" \
  --timing-simulator "$PWD/.wse_model_out/timing-build/build/timing_runner" \
  --output "$PWD/.wse_model_out/payload-timing-run"
```

`models/pto_allgather/timing_profile.json` 提供显式参数；其中的暂定项保持
`pending`。功能模型的串行执行周期和时序模型的 NoC 周期分别记录。
