#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Windows 与 Unix 的虚拟环境目录不同：Windows 用 .venv/Scripts，Unix 用 .venv/bin。
venv_python() {
  if [ -x ".venv/Scripts/python.exe" ]; then
    echo ".venv/Scripts/python.exe"
  elif [ -x ".venv/bin/python" ]; then
    echo ".venv/bin/python"
  fi
}

# 依次尝试可用的 Python，跳过 Windows 应用商店的占位别名。
pick_python() {
  for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
}

PYTHON_BIN="$(pick_python)"
if [ -z "$PYTHON_BIN" ]; then
  echo "[错误] 未找到可用的 Python，请先安装 Python 3.10+ 并加入 PATH。"
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "[错误] 未找到 Node.js，请先安装 Node.js 16+。"
  exit 1
fi

if [ -z "$(venv_python)" ]; then
  echo "[1/3] 创建虚拟环境..."
  "$PYTHON_BIN" -m venv .venv
else
  echo "[1/3] 使用已有虚拟环境 .venv"
fi

VENV_PY="$(venv_python)"
if [ -z "$VENV_PY" ]; then
  echo "[错误] 虚拟环境创建失败，请检查 Python 安装。"
  exit 1
fi

echo "[2/3] 安装 Python 依赖..."
"$VENV_PY" -m pip install --disable-pip-version-check -q -r requirements.txt

echo "[3/3] 启动控制台：http://localhost:8420/"
exec "$VENV_PY" main.py --open
