# WSE Coarse

本分支提供 `CoarseWseMachine`，用于 WSE（晶圆级系统）的 NoC（片上网络）通信仿真。
调用者提供节点、转发树、传输字节数和逻辑依赖；模型不装载实际数据内存或设备地址。

原 `WseMachine`、`TimedWseMachine` 及其构建、验证入口已从本分支移除。
详细模型保留在 `designs/WSE_model` 分支。当前分支名按用户要求为 `deaigns/WSE_coarse`。

模型使用 pyCircuit 编写。生成 C++ 后通过 GFSIM 运行，或从同一最终中间表示生成
Verilog 再通过 Verilator 运行。Python 只准备工作负载、配置和验证预期；链路争用、
组播、接收与完成时间由生成模型推进。

- 固定 5×8 网格、40 节点；按无载荷 flit 元信息逐跳转发。
- 方向链路每周期服务一个 flit，目的端所有入方向合计每周期接收一个 flit。
- 显式组播树、源队列、读写端口、Calendar 时隙许可和有限接收容量。
- `WaitGroup` 等待逻辑组完成；`Barrier` 全局排空并增加准备时间、重置时隙原点。
- 通过 `RetireGroup` 释放组接收额度。逻辑块标识随元信息保存，依赖通过组表达。

`Profile()` 采用兼容的端点时间预算，但 Calendar 默认全开放。与旧模型默认时隙
对照时，必须通过比较适配器导入同一 Calendar，不能把这两种配置混为一谈。
参数尚未经过硬件校准，输出是指定条件下的模型周期。

入口见 [RUNNING.md](RUNNING.md)，接口见 [程序接口](docs/program-interface.md)，
实现边界见 [architecture.md](docs/architecture.md)。实际验收和性能结果见
[同负载比较报告](docs/coarse-comparison.md)。旧地址程序的适配代码只服务于独立对照；新模型的输入没有设备地址。
