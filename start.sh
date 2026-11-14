#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 Python3，请先安装 Python 3.10+ 并加入 PATH。"
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "[错误] 未找到 Node.js，请先安装 Node.js 16+。"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] 创建虚拟环境..."
  python3 -m venv .venv
else
  echo "[1/3] 使用已有虚拟环境 .venv"
fi

echo "[2/3] 安装 Python 依赖..."
".venv/bin/python" -m pip install --disable-pip-version-check -q -r requirements.txt

echo "[3/3] 启动控制台：http://localhost:8420/"
exec ".venv/bin/python" main.py --open
