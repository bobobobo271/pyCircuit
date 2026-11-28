# WSE_parallel 原生 GFSIM 原型

这一版用原生 C++11 调用独立 GFSIM 的 `parallel-subtree` 执行器，先验证模型时间、争用顺序和串并行一致性，再将同一实现用于 5×8 的 40 节点网格。当前交付是有明确输入范围的通信原型。

`WSE_parallel` 分支首先记录了当前 coarse 工作区的源码快照，再新增本目录。旧工作区保持原样。`coarse-baseline.json` 记录继承的改动和文件哈希；这些继承改动与本目录的新实现分开提交。依赖固定在 `dependency.lock.json` 中的 GFSIM 提交，构建时检查版本和已跟踪文件是否干净。

## 当前输入范围

- 网格最多 40 节点；完整拓扑为 5 行、8 列。
- 每个源只有一个传输，全部传输从模型时刻 0 就绪。源按编号递增启动，与 coarse 的源扫描顺序一致。
- 路由是显式有向树，目标可以同时转发；允许本地目标及非目标叶节点。
- 每个 flit（链路传输单元）承载最多 52 B 有效数据，占用 64 B 线传量。每方向链路、每节点共享接收端口均每模型周期服务一个 flit。
- 跳延迟为可配置正整数；Calendar（链路时隙许可）全开放，端点读写、发送、接收和源释放预算为零。
- 只存通信元信息。输出包括完成事件、流量和模型结束时间；不计算张量值。

这一原型尚未接入完整 coarse 程序接口：释放时间、每源等待队列、组依赖、屏障、组退休、Calendar 配置、非零端点预算和容量故障仍属于后续工作。C++ `Flow` 接口不含这些字段。输入队列和事件堆是执行器内部结构，当前未复现 coarse 的 1024 事件容量故障。

## 执行方式

模型保留按 `(模型时间, 插入序号)` 排序的全局事件堆。父模块每次从堆首选择一个连续前缀：时间相同、源互不相同、节点互不相同。遇到第一个冲突就结束选择；完成及源释放事件单独处理。

每个节点是一个 GFSIM 子模块，通过自己的请求队列接收事件和源状态快照。节点在 Work 阶段计算，在 Xfer 阶段提交自己的链路和接收状态，再通过结果队列返回后继事件。父模块按原事件顺序归并结果，按各事件内部的生成顺序分配新序号。线程先后完成的次序不参与仲裁。

请求和结果队列各只有一个生产者、一个消费者，容量为 1。内部队列交接需要多个 GFSIM 执行步，但事件的模型时间保持原值。零延迟后继仍在原模型时刻处理；只有事件时间戳推动通信时间。

`workers=0` 不配置并行切分；正数通过 `SetParallelChildren` 和 `SetWorkerCount` 使用真实工作线程。`reference` 模式每次只取一个事件，用来检验批处理是否改变结果。它与批处理共享事件语义，独立正确性证据来自手算、流量守恒及现有 coarse 可执行文件对照。

这种保守划分首先发掘不同源、不同节点之间的并行。同源组播分支目前顺序处理。每批事件的请求／回复及阶段同步开销较高，线程数增加是否有收益必须实测。

## 构建和运行

以下命令从仓库根目录执行；需要 CMake、支持 C++11 的编译器和 Python 3.10 以上。可用 `CMAKE_CXX_COMPILER`、`CMAKE_OSX_SYSROOT` 指定本机工具链。

```sh
git clone --branch parallel-subtree --single-branch \
  https://github.com/hmljy2020/GFSIM.git .gfsim-parallel-src
git -C .gfsim-parallel-src checkout cc833bf4003aac17f0177067e70ecaffaf0d54c3
cmake -S designs/wse_parallel -B build-wse-parallel -DCMAKE_BUILD_TYPE=Release
cmake --build build-wse-parallel --target wse_parallel_runner -j 4
ctest --test-dir build-wse-parallel -R '^wse_parallel_' --output-on-failure
```

输入文本第一行是 `行数 列数 跳延迟 传输数`。每个传输依次给出 `ID 源节点 字节数 目标数 树边数`、目标列表和每条边的两个节点。例如 200 B 从 0 经 1 到 2：

```text
1 3 1 1
7 0 200 1 2
2
0 1
1 2
```

```sh
./build-wse-parallel/wse_parallel_runner input.txt 4 batch
./build-wse-parallel/wse_parallel_runner input.txt 0 reference
```

输出为 JSON。`model_time` 是包括网络排空在内的模型结束时间，`execution_steps` 是 GFSIM 执行步数；`run_seconds` 只计 Step 循环，`build_seconds` 单独记录系统构建和初始重置。目标写完成可能早于网络排空。末尾加 `no-trace` 可关闭事件保存。

## 验证和测量

`tests/verify.py` 的最小阶段覆盖单跳、多跳、跳延迟对照、独立流、跨入口接收争用、共享链路、组播和本地完成后的尾支排空。40 节点阶段覆盖全节点双向成对交换、全网树广播、共享链路和节点 33 路径。

每个场景比较单事件串行、批处理串行和 1/2/4/8 工作线程的完整事件及统计，并检查独立手算完成时间、每条树边的 flit 数和每周期资源占用。传输 ID 与输入次序刻意不同，检查仲裁确实跟随源扫描和事件插入顺序。

加 `--coarse-runner /absolute/path/coarse_runner` 可将小网格嵌入 coarse 的 5×8 网格，生成相同语义的输入，运行旧模型并逐项比较完整事件。此对照使用已有且记录哈希的 coarse 可执行文件；本轮只重新构建原生模型，没有重建 pyCircuit 编译链。

```sh
python3 designs/wse_parallel/tests/verify.py \
  --runner "$PWD/build-wse-parallel/wse_parallel_runner" \
  --stage prototype --output build-wse-parallel/check-prototype
python3 designs/wse_parallel/tests/verify.py \
  --runner "$PWD/build-wse-parallel/wse_parallel_runner" \
  --stage mesh40 --output build-wse-parallel/check-mesh40
python3 designs/wse_parallel/tests/benchmark.py \
  --runner "$PWD/build-wse-parallel/wse_parallel_runner" \
  --output build-wse-parallel/benchmark
```

测速关闭 trace，使用同一可执行文件和输入，轮换线程配置顺序。每配置丢弃前 2 次运行，再保留 7 次样本，报告中位数及范围。每个样本创建新的进程和系统，计时的 Step 循环内复用线程；系统构建与进程总时间另外保存。这是原型扩展性实验，不是跨 Reset 复用系统的稳态测试。正式采样应单独运行，避免并行构建和回归测试干扰。

实际执行记录及本轮结果见 [validation.md](validation.md)。后续优化优先减少一次语义动作所需的全局同步次数、扩大节点内的有效工作量；先保留上述一致性检查，再考虑放宽同源事件的并行限制。
