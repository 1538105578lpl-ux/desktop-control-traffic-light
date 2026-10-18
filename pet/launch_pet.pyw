# -*- coding: utf-8 -*-
"""双击这个文件即可启动桌宠（不需要 bat，也不会有黑框）。"""
"""若双击无效，说明 .pyw 没关联到 Python，改用 launch_pet.bat。"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "traffic_light.py")

if __name__ == "__main__":
    sys.argv = [TARGET]
    runpy.run_path(TARGET, run_name="__main__")
