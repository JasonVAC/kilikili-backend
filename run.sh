#!/bin/bash
# 单端口启动（配合一条反向隧道）
cd "$(dirname "$0")"
exec python3 server.py single "${1:-8083}"
