# 当前模型结构

架构设计以 [白皮书](design/wse-system-architecture-whitepaper.md) 和
[Calendar 方案](design/wse-calendar-scheme.md) 为输入；源码中未确定的设计值
由 `src/wse_model/open_items.py` 显式列出。

## 两层模型

`src/wse_model/` 是普通 Python 语义基准。`topology.py` 描述二维拓扑；
`calendar/` 描述路由表、轮次、地址和接收规则；`noc/` 描述转发与配置；
`core/`、`host/`、`analysis/` 提供计算、存储、宿主和性能分析。
`compiler/` 包含产物约束检查及受限 PTO AllGather 到 Calendar 的映射。

`models/` 使用 pyCircuit 描述电路状态、规则和连接。pyCircuit 编译各源单元、
链接完整 ACIR，再生成 C++ 或 Verilog。独立预期保留在 `tools/` 和 `tests/`，
不参与被测电路的代码生成。

## Calendar 控制闭环

`models/calendar/allgather_control.py` 连接 32 个独立成员。
每个成员拥有配置准入、轮次、路由项解码和有限接收账户；8 个共享无状态
转发实例服务当前行或列，一次处理一个多播片段。

协调器按实际成员反馈执行：配置就绪、推进轮次、建立接收、发送、等待全部
完成并排空、退役，再切换到下一阶段。所有目的节点接纳前保持片段身份；
成员故障或轮次不一致会停止推进。

该入口处理地址、长度和身份等片段元数据。它的串行调度不提供物理链路延迟
或并发仲裁模型。`allgather.py` 还保留固定输入序列的回归入口。

## 实际载荷与暂定时序

`models/pto_allgather/payload.py` 为每个成员分配 64 KiB 字节存储。
运行时描述符驱动实际读取和写入，列阶段读取行阶段的真实输出。
`examples/pto_allgather.py` 生成 PyPTO 程序；`tools/lower_pto_allgather.py`
分析 PTOAS 处理后的 IR，再由 `src/wse_model/compiler/allgather.py` 映射到
WSE 路由、地址与片段。该路径只接纳固定形状、按序、单片段的 AllGather。

`models/pto_allgather/timing.py` 是独立的阶段时序模型。
`src/wse_model/analysis/allgather_schedule.py` 根据同一工作负载和显式时序配置
推导调度，生成模型执行这些阶段，并与独立期望比较。载荷功能步数和时序
周期分别统计。时序参数中的 `pending` 是暂定假设，不能当作硬件已确定参数。

矩阵、向量和完整 FFN/Attention 的数值计算仍未实现；普通 Python 中的
Cube/Vector 代码用于容量、利用率与时延估算。
