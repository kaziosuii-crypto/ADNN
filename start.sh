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
  echo "[1/4] 创建虚拟环境..."
  "$PYTHON_BIN" -m venv .venv
else
  echo "[1/4] 使用已有虚拟环境 .venv"
fi

VENV_PY="$(venv_python)"
if [ -z "$VENV_PY" ]; then
  echo "[错误] 虚拟环境创建失败，请检查 Python 安装。"
  exit 1
fi

echo "[2/4] 安装 Python 依赖..."
"$VENV_PY" -m pip install --disable-pip-version-check -q -r requirements.txt

# 编译 SMCGP14 的 C 实现：有编译器就编译，没有就回退纯 Python 的 Node 实现。
echo "[3/4] 编译 SMCGP14 神经元引擎..."
C_SRC="c/smcgp_node.c"
C_LIB="c/libsmcgp_node.so"
CC_BIN=""
for candidate in clang gcc cc; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CC_BIN="$candidate"
    break
  fi
done
if [ ! -f "$C_SRC" ]; then
  echo "  [警告] 缺少 $C_SRC，将回退纯 Python 实现。"
elif [ -z "$CC_BIN" ]; then
  echo "  [警告] 未找到 C 编译器（clang/gcc/cc），将回退纯 Python 实现。"
elif [ -f "$C_LIB" ] && [ "$C_LIB" -nt "$C_SRC" ]; then
  echo "  $C_LIB 已是最新，跳过编译。"
else
  if "$CC_BIN" -O3 -fPIC -shared -pthread -o "$C_LIB" "$C_SRC"; then
    echo "  已用 $CC_BIN 编译 $C_LIB"
  else
    # 编译失败不阻断启动：删掉半成品，nodeManager 会自动回退纯 Python 实现。
    rm -f "$C_LIB"
    echo "  [警告] 编译失败，将回退纯 Python 实现。"
  fi
fi

echo "[4/4] 启动控制台：http://localhost:8420/"
exec "$VENV_PY" main.py --open
