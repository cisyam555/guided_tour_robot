#!/usr/bin/env bash
# 一键启动导览，并把终端输出同时写入 log/tour_run_*.log，便于排查。
# 注意：不要开 set -u，ROS 的 setup.bash 会引用未定义变量而直接报错。
set +u
set -o pipefail  # tee 保存日志时仍返回启动程序的失败状态
cd "$(dirname "$0")"

source /opt/ros/jazzy/setup.bash
source /home/nvidia/xos/setup.bash
source install/setup.bash

# IDE 终端可能注入自己的库路径，导致 GLIBCXX 冲突，这里只剔除相关条目。
export LD_LIBRARY_PATH=$(printf '%s' "$LD_LIBRARY_PATH" | tr ':' '\n' \
    | grep -v 'trae-cn-server' | paste -sd:)

mkdir -p log
LOG="log/tour_run_$(date +%Y%m%d_%H%M%S).log"
echo "运行日志：$(pwd)/$LOG"
echo "启动参数：$*"

ros2 launch learn1_d tour.launch.py "$@" 2>&1 | tee "$LOG"
