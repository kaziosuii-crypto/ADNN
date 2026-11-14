# Pong 卡顿诊断

- Status: [COMPLETED]
- progress_status: verified
- 项目: E:\BtB
- 会话: pong-lag
- 范围: 初始诊断完成；按后续要求撤回调度优化，仅解耦数据库并增加控制、监控窗口。
- Evidence Source: HTTP Debug Server
- Evidence Directory: C:\Users\Tsuba\AppData\Local\Temp\btb-pong-lag
- Log File: C:\Users\Tsuba\AppData\Local\Temp\btb-pong-lag\trae-debug-log-pong-lag.ndjson
- Env File: C:\Users\Tsuba\AppData\Local\Temp\btb-pong-lag\pong-lag.env

## 假设

| ID | 假设 | 可能性 | 成本 | 预期信号 | 结论 |
|---|---|---|---|---|---|
| A | 每帧启动 800 线程阻塞 UI | 高 | 低 | 普通帧 Thread.start 累计约 110–180ms | CONFIRMED |
| B | 数据库等待经管理器锁传递到 UI | 高 | 中 | 慢帧 2.37–3.45 秒，主要耗在 getNode 等锁 | CONFIRMED |
| C | 多 Tk 跨线程通信主导本次复现的卡顿 | 中 | 中 | 卡顿时未发生 set_color 回调；两状态窗口线程在 mainloop | REJECTED |
| D | after(200) 叠加处理耗时，实际低于 5fps | 高 | 低 | 普通帧间隔约 0.33–0.40 秒，并有数秒停顿 | CONFIRMED |

## 复现

使用外部诊断脚本包装计时函数。对比纯 Pong 与 main.py 三窗口运行。
使用现有数据库的 SQLite 备份，不修改原数据库。
记录帧耗时、线程启动耗时、节点获取耗时、DB 锁等待和服务耗时、线程栈。

## 日志证据

NDJSON 每行一个事件：

- 第 1–3 行：纯 Pong，运行约 5 秒，24 帧，处理每帧约 1.1–1.9ms。
- 第 4 行：完整 main.py，20.17 秒，CPU 时间 10.02 秒，24 个逻辑 CPU，GIL 开启。
  完成 24 帧，另有一帧在截止时阻塞。主线程 getNode 累计约 8.01 秒，
  Thread.start 累计约 3.09 秒。DB 锁等待平均约 1.38 秒，
  DB 锁内服务平均约 3.16ms。等待时间跨线程重叠，不能当作墙钟时间相加。
- 第 5 行：第 7、13、19 帧处理约 2373、2524、3449ms，
  对应 getNode 累计约 2245、2387、3313ms。
- 第 6 行：100 次主线程采样中，59 次在 nodeManager.py:50 等管理器锁，
  16 次在 Thread.start 等待新线程启动，25 次在 Tk mainloop。
- 第 7–9 行：第二轮运行约 12 秒，重复出现约 3.26 秒慢帧。
  第 9 行的 2.629 秒采样显示 719 个线程在 saveNodeToDatabase 等 DB 锁，
  一个线程沿 forward -> getNode -> getNodeFromDatabase 等 DB 锁，
  同时主线程在 getNode 的管理器锁处等待。

## 原因

1. 主要原因：数据库等待通过管理器锁阻塞 UI。
   getNode 在整个缓存查询和数据库读取期间持有 self._lock。
   后台传播访问未缓存节点时，拿着 self._lock 等 self._db_lock；
   主线程即使读取已缓存视觉节点，也必须等待 self._lock。
   800 个节点窗口接近同时到期，逐节点 execute + commit 形成写入队列。
   这是锁竞争和队列阻塞，不是本次已证实的死锁。
2. 次要原因：每帧在 UI 线程连续创建 800 个 OS 线程。
   已激活节点虽然迅速返回，但新线程仍先创建；首次激活还会 sleep(2)。
   两次实验出现约 800–1467 个同时存活线程。
3. 固定延时叠加：_tick 完成所有工作后再 after(200)，
   实际周期是处理耗时加 200ms，不是恒定 200ms。
4. 绘图并非主要瓶颈。GPU 没有参与 mpmath / SQLite / Python 线程计算。
   完整运行 CPU 时间约为墙钟时间的一半，在 24 逻辑 CPU 上折合整机约 2.1%。
   锁等待、磁盘等待、sleep 不会持续占用 CPU，GIL 也限制纯 Python 多线程并行。

## 建议顺序

1. 已采用：启动时全量加载数据库，运行期间节点查询只访问内存。
2. 已采用：后台批量持久化，停止后完整落盘；SQLite 写锁保留。
3. 已撤回：统一激活事件调度、同帧统一到达时间戳。模型依赖实际异步时序，
   必须保留各节点独立线程、独立 sleep(timeWindow) 和窗口内的实际输入顺序。
4. 已撤回：按目标时刻补偿帧周期。恢复每次处理结束后 after(200)。
   保留原有线程开销与时序抖动，不再以减少这些抖动作为优化目标。

## 当前实现与验证

- `node.py` 恢复独立线程窗口；没有共享调度器、批量激活时间戳或线程池。
- `nodeManager.py` 一次性读取全部持久化节点。已有的 804 个特殊节点也保留其值与连接。
  `save_interval=2.0` 表示每 2 秒后台写入；设为 `None` 则仅在停止时落盘。
  adapter 中可修改 `SAVE_INTERVAL`。
- 停止请求立即禁止新的激活和传播，等待已进入窗口的线程退出后保存所有节点。
  不使用强杀线程，也不补完被取消窗口的计算。等待时间包含剩余 sleep 与落盘耗时。
- 界面统一在 Tk 主线程，但神经元线程保持独立。原来的三个窗口保留，
  新增只有停止按钮的控制窗口和全网络实时状态窗口。
- 状态窗口每 200ms 读取一次观察快照，不给整个网络增加同步屏障。
  金色边框标注 adapter 的 804 个节点；当前 active 为绿，否则为红。
  其余所有内存节点在下方可滚动区域显示，点击任意节点可看值与前向连接。
- 7 项专项测试通过：全量恢复、运行期不读库、独立时序及窗口累积、
  停止取消传播并保存、异步/退出保存模式、全节点与特殊颜色、按钮异步保存。
- main.py 整体验证通过：5 个窗口，804 个特殊节点，停止前 775 个激活节点；
  停止后 1208 个内存节点全部转红，值和连接与 SQLite 逐一一致，
  integrity_check=ok，状态停止变化，退出后仅剩主线程，无异常。
- 所有测试均使用临时数据库或原库的只读备份，未修改工作区 nodes.db。
- 此前统一调度版本的性能数据不代表当前实现，也不能用作时序等价性的证明。

## 最终证据门

- Evidence Sources: 上述 NDJSON 第 1–9 行，三轮运行。
- progress_status: verified（数据库解耦与新功能；不代表消除了所有原始线程开销）
- A: CONFIRMED；B: CONFIRMED；C: REJECTED（仅限已复现停顿）；D: CONFIRMED。
- 未覆盖：长期运行、状态回调密集触发时的性能、用户原进程的精确耗时。
- 初始诊断没有修改业务代码；后续变更及其约束见“当前实现与验证”。
- 新增验证脚本位于外部临时证据目录：test_timing_monitor.py、test_main_windows.py。
- 测试进程已退出；保留历史诊断证据以便复核。
