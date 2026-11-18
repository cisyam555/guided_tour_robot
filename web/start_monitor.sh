#!/usr/bin/env bash
set -e
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ " $* " != *" --preview "* ]]; then
  source /opt/ros/jazzy/setup.bash
  source /home/nvidia/xos/setup.bash
fi
exec /usr/bin/python3 monitor.py "$@"
