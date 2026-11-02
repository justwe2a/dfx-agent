"""Run one command with a temporary Windows system/display idle-sleep request."""
import ctypes
from ctypes import wintypes
import os
import subprocess
import sys


def run_awake(argv):
    if os.name != 'nt' or not argv:
        raise ValueError('Usage on Windows: python scripts/keep-awake.py <command> [args...]')
    set_state = ctypes.WinDLL('kernel32', use_last_error=True).SetThreadExecutionState
    set_state.argtypes = [wintypes.DWORD]
    set_state.restype = wintypes.DWORD
    # No permanent power-plan change; the request belongs to this waiting thread.
    if not set_state(0x80000003):
        raise OSError('Windows rejected the temporary system/display awake request')
    print('AWAKE request active for this command', flush=True)
    try:
        return subprocess.run(argv, check=False).returncode
    finally:
        if not set_state(0x80000000):
            raise OSError('Windows did not confirm release of the awake request')
        print('AWAKE request released', flush=True)


if __name__ == '__main__':
    raise SystemExit(run_awake(sys.argv[1:]))
