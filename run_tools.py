#!/usr/bin/env python3
import os
import sys
import subprocess


current_dir = os.path.dirname(os.path.abspath(__file__))


os.environ['PYTHONPATH'] = current_dir
command = [sys.executable, '-m', 'src.cli.tools']


if len(sys.argv) > 1:
    command.extend(sys.argv[1:])
else:
    command.extend(['system', 'shell'])


try:
    result = subprocess.run(command, cwd=current_dir)
    sys.exit(result.returncode)
except Exception as e:
    print(f"Ошибка запуска: {e}")
    sys.exit(1)