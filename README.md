# ADNN

一个以“节点激活窗口 + 连接权重”为核心的神经网络实验项目，内置 Pong 闭环演示和浏览器控制台。

- Python 负责网络计算与持久化，Node.js 负责网页渲染与状态推送。
- 节点完成后按连接权重决定是否向下游传播，权重与会随时间衰减并写入 SQLite。
- 支持多进程 worker 计算（Windows `spawn`），浏览器通过 SSE 实时看到画布、激活状态与网络图。

## 功能

- **Pong 演示**：游戏网格里点亮的像素会投递给对应视觉神经元，神经元的输出反过来驱动球拍。
- **浏览器控制台**：Pong 画面、操作指示灯与总体统计（节点数 / 内部节点数 / 帧率）。
- **启停控制**：页面上的“启动网络”“强制结束网络并落盘”按钮；停止时会把全部神经元的 SMCGP14 图结构写入 `nodes.db` 的 `smcgp_nodes` 表。
- **SMCGP14 神经元**：每个神经元是一张可自修改的 SMCGP14 计算图（14 种指令），C 实现（`c/smcgp_node.c`）通过 ctypes 与 Python 互操作，缺少库时自动回退纯 Python。
- **无前端构建步骤**：网页是原生 HTML / CSS / JavaScript，Node 端只使用标准库，无需 `npm install`。

## 目录结构

```
ADNN/
├─ c/
│  └─ smcgp_node.c   SMCGP14 神经元的 C 实现（编译成 libsmcgp_node.so）
├─ compute/          网络核心
│  ├─ node.py        SMCGP14 神经元（纯 Python 参考实现）
│  ├─ nodeManager.py 神经元群体：调度、路由、落盘（优先调用 C 库）
│  └─ test_smcgp14.py SMCGP14 边界单测
├─ demo/pong/
│  ├─ pong.py        Pong 规则与网格采样
│  ├─ pong_adapter.py 网络与游戏的适配层
│  └─ bridge.py      Python → Node 的 JSON 行桥接进程
├─ web/
│  ├─ server.js      静态页面 + SSE + 桥接进程管理
│  └─ public/        控制台页面
├─ main.py           入口：启动 Node 控制台
├─ start.bat         Windows 一键启动
├─ requirements.txt
└─ nodes.db          运行时自动生成（不纳入版本控制）
```

## 环境要求

- Windows 10 / 11（一键脚本针对 Windows；其他系统可按手动步骤运行）
- Python 3.10+
- Node.js 16+

## 一键启动

双击 `start.bat`。脚本会自动：

1. 检查 Python 与 Node.js；
2. 首次运行时创建 `.venv` 虚拟环境；
3. 安装 `requirements.txt` 中的依赖；
4. 启动控制台并打开 <http://localhost:8420/>。

打开页面后，点击右上角 **启动网络** 才开始计算。

## 手动启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

如果希望启动后自动打开浏览器：

```powershell
.\.venv\Scripts\python.exe main.py --open
```

## 使用说明

- 滚轮缩放网络图，拖拽平移，点击或悬停节点查看数值与前向连接。
- “数值”勾选后会在节点旁显示当前值；“适应窗口”恢复全局视图。
- 停止网络会等待 worker 退出并把状态写回 `nodes.db`，完成后按钮显示“网络已结束，已落盘”。

## 配置

通过环境变量调整：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BTB_PORT` | `8420` | 控制台监听端口 |
| `BTB_DB` | `nodes.db` | 节点数据库路径 |
| `BTB_PYTHON` | `.venv` 内的 Python | 覆盖桥接进程使用的解释器 |

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest compute.test_smcgp14
```

## 数据与重置

- 首次运行会自动创建 `nodes.db`，其中 `smcgp_nodes` 表保存每个神经元的 SMCGP14 图结构。
- 停止网络后全部图结构会落盘；下次启动会从该文件恢复。
- 删除 `nodes.db` 即可从空网络重新开始。

## 备注

- 页面上的“启动网络”按钮是必要的：控制台本身只负责展示，不会自动开始计算。
- 若端口被占用，可先设置 `BTB_PORT` 再启动。
