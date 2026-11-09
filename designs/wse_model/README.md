# WSE Model

WSE 架构语义与 pyCircuit 通信模型的完整源码快照，来源为
[PTO-ISA/wse-model](https://github.com/PTO-ISA/wse-model)。
本目录作为独立 SDK 消费项目使用，不注册到 pyCircuit 的示例 smoke 门禁。

## 实现范围

| 层次 | 内容 |
| --- | --- |
| 普通 Python 语义核心 | 拓扑、Calendar 编码与合法性、路由、接收、容量、编译产物约束和宿主运行规则；Cube/Vector 与存储的容量、时延估算 |
| Calendar 电路 | 配置准入、轮次、路由解码、转发、有限接收状态，以及按实际完成状态推进的 32 成员 AllGather |
| PTO AllGather 载荷模型 | PyPTO 生成 PTO IR、PTOAS 处理、Calendar 描述符转换，以及具有实际字节存储的 pyCircuit 模型和独立输出比较 |
| 暂定时序模型 | 显式参数下的 UB 服务、Calendar 时窗、链路与端点调度；参数的 `pending` 状态保留在配置中 |

Calendar 接收/控制模型与 PTO 载荷模型是两个独立入口。前者验证通用接收
规则及反馈闭环；后者实现受限、已建立接收、按序单片段传输的载荷流程。
矩阵乘法、向量算子和完整 FFN/Attention 数值计算尚未接入电路。

## 目录

```text
src/wse_model/       普通 Python 语义、架构分析、CLI 与 PTO 到 Calendar 的映射
models/calendar/    pyCircuit 通用通信模块、AllGather 闭环、测试输入、NDF 规范
models/pto_allgather/  载荷模型、时序模型、C++ 驱动和显式时序参数
tools/              编译、运行、IR 检查及独立比较入口
tests/              单元、契约、黄金向量及集成测试代码
examples/           普通 Python 示例和 PyPTO AllGather 输入
schemas/            路由表和内核产物的 JSON schema
docs/design/        按原始字节保留的架构白皮书与 Calendar 方案
docs/architecture.md  当前模块关系与模型边界
docs/profiles.md    当前测试配置和有限资源语义
RUNNING.md          依赖与运行方法
```

## 普通 Python 使用

从本目录执行，使用 Python 3.11 或更新版本：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
make check
python -m wse_model --json topology show
python examples/ffn_allgather.py
```

纯 Python 检查不需要 pyCircuit、PyPTO 或 PTOAS。
原生编译和实际载荷执行见 [RUNNING.md](RUNNING.md)。

## 编译器依赖

`models/calendar/pycircuit.lock.json` 记录所需的 pyCircuit 集成版本
`7942030dd623bb8db6a0a3bcb061fa11f667f43a`，LLVM/MLIR 为 22.1.8。
该集成提交目前只存在于本机，尚未推送；fork 的 `main` 编译器不包含其中全部
修复。原生执行需要另外取得该集成 checkout 及其构建的 SDK。
本快照只发布 WSE 项目，不携带编译器补丁或工具链。

设计源码、运行脚本和测试代码随项目提供；生成的 ACIR/C++/Verilog、仿真程序、
历史迁移记录、构建日志和验证结果均不纳入此快照。

## 许可

软件使用 BSD 3-Clause，见 [LICENSE](LICENSE)。设计文档的权利说明见
[NOTICE](NOTICE)。
