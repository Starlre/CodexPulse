from __future__ import annotations
import ctypes
from ctypes import wintypes
import subprocess
import sys
import time
from pathlib import Path

import psutil

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
PULSE = BASE / 'CodexPulse.exe'
CODEX_ROOT = '\\openai\\codex\\bin\\'
MUTEX_NAME = 'Local\\CodexPulseMonitor'


def codex_is_running() -> bool:
    for process in psutil.process_iter(['name', 'exe']):
        try:
            if (process.info.get('name') or '').casefold() != 'codex.exe':
                continue
            executable = process.info.get('exe') or process.exe()
            if CODEX_ROOT in executable.replace('/', '\\').casefold():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    return False


def pulse_is_running() -> bool:
    target = str(PULSE).casefold()
    for process in psutil.process_iter(['name', 'exe']):
        try:
            if (process.info.get('name') or '').casefold() == 'codexpulse.exe':
                if (process.info.get('exe') or process.exe()).casefold() == target:
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    return False


def run():
    if not PULSE.is_file():
        return
    if sys.platform == 'win32':
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == 183:
            return
    while True:
        try:
            if codex_is_running() and not pulse_is_running():
                subprocess.Popen([str(PULSE)], cwd=str(BASE), close_fds=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(3 if codex_is_running() else 8)
        except (OSError, psutil.Error):
            time.sleep(8)


if __name__ == '__main__':
    run()
