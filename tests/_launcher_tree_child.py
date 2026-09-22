"""测试用长驻子进程：写就绪/pid 文件后保持运行，等待被结束。"""
import os
from pathlib import Path
import sys
import time

signal_file, pid_file = Path(sys.argv[1]), Path(sys.argv[2])
signal_file.write_text("ready", encoding="utf-8")
pid_file.write_text(str(os.getpid()), encoding="utf-8")
time.sleep(300)
