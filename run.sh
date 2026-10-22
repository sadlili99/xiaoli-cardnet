#!/bin/sh
# 小黎卡网启动脚本：PORT 环境变量可改端口，默认 8088
cd "$(dirname "$0")"
exec python3 -u server.py
