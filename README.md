# ADNN

一个以“节点激活窗口 + 连接权重”为核心的神经网络实验项目，内置 Pong 闭环演示和浏览器控制台。

- Python 负责网络计算与持久化，Node.js 负责网页渲染与状态推送。
- 节点完成后按连接权重决定是否向下游传播，权重与会随时间衰减并写入 SQLite。
- 支持多进程 worker 计算（Windows `spawn`），浏览器通过 SSE 实时看到画布、激活状态与网络图。

## 功能

- **Pong 演示**：游戏网格的一次采样会激活对应视觉节点，网络的 UP / DOWN / sense 输出反过来驱动球拍。
- **浏览器控制台**：左侧 Pong 画面与操作指示灯，右侧可缩放、拖拽、搜索的节点网络图。
- **启停控制**：页面上的“启动网络”“强制结束网络并落盘”按钮；停止时会把节点状态与连接写入 `nodes.db`。
- **多进程计算**：节点按 ID 分片到多个 worker 进程，跨分片传播通过进程队列完成，主进程统一落盘。
- **无前端构建步骤**：网页是原生 HTML / CSS / JavaScript，Node 端只使用标准库，无需 `npm install`。

## 目录结构

```
ADNN/
├─ compute/          网络核心
│  ├─ node.py        单节点：激活窗口、数值计算、向下游传播
│  ├─ nodeManager.py 单进程版管理器（线程模型）
│  ├─ mp_manager.py  多进程版管理器（当前控制台使用）
│  └─ mp_smoke.py    多进程冒烟测试
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
.\.venv\Scripts\python.exe -m compute.mp_smoke
```

## 数据与重置

- 首次运行会自动创建 `nodes.db`，其中保存节点数值与前向连接。
- 停止网络后数据会落盘；下次启动会从该文件恢复。
- 删除 `nodes.db` 即可从空网络重新开始。

## 备注

- 页面上的“启动网络”按钮是必要的：控制台本身只负责展示，不会自动开始计算。
- 若端口被占用，可先设置 `BTB_PORT` 再启动。
