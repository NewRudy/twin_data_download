#!/usr/bin/env bash
# setup_env.sh — 在新机器上一键初始化影像下载环境
# 用法: bash setup_env.sh
# 可选环境变量:
#   VENV_IMAGERY  venv 路径 (默认 ~/.venv_imagery)
#   PIP_INDEX     pip 源 (默认清华源, 国内机器友好; 海外机器可改官方源)
set -euo pipefail

VENV="${VENV_IMAGERY:-$HOME/.venv_imagery}"
INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x "$VENV/bin/python" ]; then
  echo "[*] 创建虚拟环境: $VENV"
  python3 -m venv "$VENV"
fi

echo "[*] 安装依赖 (pip 源: $INDEX)"
"$VENV/bin/pip" install --upgrade pip -i "$INDEX"
"$VENV/bin/pip" install -r "$HERE/requirements.txt" -i "$INDEX"

echo ""
echo "[✓] 环境就绪: $VENV"
echo "    运行示例:"
echo "    $VENV/bin/python $HERE/scripts/download_scene.py --lon 114.351205 --lat 26.937214 --radius 3000"
