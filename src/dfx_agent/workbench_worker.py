"""CLI child with a local control pipe; no second agent loop or request replay."""
import _thread
import json
import os
from pathlib import Path
import signal
import sys
import threading


def main():
    from .cli import main as cli_main
    from .workspace import save_json
    receipt = Path(sys.argv[1])
    finished = threading.Event()
    signal.signal(signal.SIGINT, signal.default_int_handler)
    # The control pipe belongs only to this worker. Git and tool children must
    # not inherit it or compete with the reader for Windows pipe operations.
    control_input = os.fdopen(os.dup(sys.stdin.fileno()), 'r', encoding='utf-8')
    empty_input = open(os.devnull, 'r')
    os.dup2(empty_input.fileno(), sys.stdin.fileno())
    if os.name == 'nt':
        import ctypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
        if not kernel.SetStdHandle(-10 & 0xffffffff, msvcrt.get_osfhandle(sys.stdin.fileno())):
            raise ctypes.WinError(ctypes.get_last_error())

    def control():
        message = control_input.readline().strip()
        if finished.is_set():
            return
        if message not in ('STOP', ''):
            print('Unknown workbench control message', file=sys.stderr, flush=True)
            return
        print('Workbench interrupt: ' + ('requested stop' if message else 'control pipe closed'),
              file=sys.stderr, flush=True)
        # Existing command execution observes its single deadline in short
        # waits so Python can deliver this interrupt before a long command ends.
        # Other blocking I/O still obeys its original timeout; no forced kill.
        _thread.interrupt_main()

    threading.Thread(target=control, daemon=True).start()
    code = 1
    try:
        code = cli_main(sys.argv[2:])
    except KeyboardInterrupt:
        print('Interrupted before CLI final result', file=sys.stderr, flush=True)
        code = 130
    finally:
        finished.set()
        try:
            save_json(receipt, {'returncode': code})
        except OSError as exc:
            print(f'Cannot save worker exit receipt: {exc}; execution was not repeated.', file=sys.stderr, flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
