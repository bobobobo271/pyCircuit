# 当前模型结构

架构输入为[白皮书](design/wse-system-architecture-whitepaper.md)和
[Calendar 方案](design/wse-calendar-scheme.md)。未确定的设计值由
`src/wse_model/open_items.py` 记录。当前通信功能模型分为共享硬件、运行时程序
和验证三层。

## 共享硬件

`models/machine/machine.py` 的 `WseMachine` 是运行时程序的数据执行入口。
它拥有程序计数器、命令状态、存储服务和逐跳路由遍历状态，按程序执行配置、
推进轮次、建立接收、复制、多播、等待和退休。

`models/machine/node.py` 的 `NodeStore` 按节点编号读取和写回一份上下文，保存配置、各类别轮次和接收账户。指令指定配置或接收操作的节点，COPY 取指时选择源，预检和写入末拍选择当前落点。宿主可同时查询另一个节点的账户摘要。若查询和写入指向同一节点，返回本拍更新后的值。

每个节点有 56 个去重槽，22 位身份由 6 位源节点号和 16 位序号组成。每槽用 4 字节存储，预检只比较目的节点当前有效的槽，末拍只写新增的槽。退休时将有效槽数归零。路由遍历使用共享位图记录待访问节点和来路方向。

整机使用 `models/calendar/forward.py` 的 `ForwardHop` 判定单跳。独立 Calendar 组件使用 `ReceiveLedger`、`EpochLedger`、`CalRegLedger` 测试早到交付、接收、轮次和配置。整机运行时程序只能在 POST 和预检后交付数据，因此账户摘要中的早到项数始终为零。

功能配置包含 40 个逻辑私有节点 bank，每个 64 KiB。内部使用一份字节存储，
按节点划分不重叠区域；公开接口仍是“节点编号＋节点内地址”。跨节点复制由
COPY 命令和逐跳转发结果决定。这里采用单片段、串行存储服务，描述的是功能
载体的存储表示和执行顺序，不赋予物理集中 RAM 或共享端口的性能含义。

接收范围、轮次、身份及所有远端落点在写入前预检。每个写拍（64 字节宽拍或 8 字节窄拍）检查完整
固定远端落点集合的动态许可；缺许可时保持当前进度。目标末拍写入、去重身份
插入和接收计数在同一事务提交。普通协议故障锁存并允许诊断读回；模型容量
断言沿用后端终止语义。详细接口以 [program-interface.md](program-interface.md)
为准。

## 运行时程序

`src/wse_model/programs/abi.py` 定义 64 字节通用指令、内存初始化和读回范围。
ABI 是程序与模拟器的数据接口。程序的实际条数、字节内容与初始内存均从宿主
加载，不进入硬件编译身份。

`src/wse_model/programs/allgather.py` 决定 AllGather 的成员、rank 顺序、分片、
地址、期望接收量与阶段依赖。`examples/pto_allgather.py` 生成真实 PyPTO 输入，
PTOAS 处理后由 `tools/lower_pto_allgather.py` 分析中间表示中的传输和同步语义。
`src/wse_model/compiler/allgather.py` 将已证明的传输映射为路由与布局片段，再
生成通用程序。硬件不直接解释任意 PTO 指令。

基线程序仍使用 32 成员、行列两阶段和 512 个 COPY 片段；这些数值属于该程序。
树形多播等其他程序通过同一接口加载，用来验证逐跳分支、只转发节点和不同
地址长度。程序复用应比较同一最终 ACIR 的哈希；ACIR 是双方后端共用的电路
中间表示。

## 验证与保留资料

项目根 `ndf.yaml` 同时读取共享组件规范与 `spec/programmed-machine.md`，
从项目根扫描实际 machine、组件和验证工具的源码绑定。`models/calendar/`
保留仅覆盖组件的独立配置；该子目录门禁不覆盖兄弟目录中的机器。NDF
（规范文档格式）追溯检查核对条款身份与源码定义绑定，行为正确性另由执行
测试验证；当前条款仍为 draft。

`tools/` 和 `tests/` 保存独立预期。输出比较从原始输入与算法定义求答案，
不参与硬件构建或数据写回。组件门禁验证共享语义，通用机器测试验证程序执行，
PTO 整链测试验证实际编译输入到输出字节的连接；三者分别记录结果。

旧 `allgather.py`、`allgather_control.py` 及对应 bench 已移入
`verification/fixtures/calendar/`。它们保留历史元数据场景和源码参考，静态
路由/metadata 检查仍可使用；`verify_calendar.py` 不再注册这些旧硬件入口。
新的 AllGather 功能验收使用 `WseMachine` 加载程序。

`models/pto_allgather/timing.py` 与对应驱动、配置保留为 analysis-only 的
行列专项时序投影。`src/wse_model/analysis/allgather_schedule.py` 按显式服务
参数推导该投影，暂定项继续标为 `pending`。这条路径与通用机器的实际字节执行
分别验证、分别统计周期。矩阵与向量的 Python 模型仍提供容量、利用率和时延
估算。

验收回执分别绑定源码、程序、最终 ACIR 和模拟器的哈希。构建、执行与
逐字节比较分别记录；同硬件复用检查固定模拟器身份，只改变运行时输入。
