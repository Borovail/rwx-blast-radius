"""Opt-in isolated-test line/read tracing, inherited by Python subprocesses.

Only activated by BLAST_TRACE_DIR and BLAST_TRACE_REPO. This is a development
coverage hook, never loaded by the runtime selector. Reports attest both entry
and clean exit; an absent/invalid child report prevents dependency refinement.
"""

import atexit
import json
import os
from pathlib import Path
import sys
import time


if os.environ.get("BLAST_TRACE_DIR") and os.environ.get("BLAST_TRACE_REPO"):
    _directory = Path(os.environ["BLAST_TRACE_DIR"])
    _root = os.path.realpath(os.environ["BLAST_TRACE_REPO"]) + os.sep
    _token = os.environ["BLAST_TRACE_TOKEN"]
    _pid = os.getpid()
    _lines = {}
    _readers = set()
    _children = {}
    _errors = []
    _busy = False
    _finished = False
    _spawning = 0
    _filenames = {}
    _monitoring = sys.monitoring
    _tool_id = 5
    _tool_name = "blast-isolated-refinement"

    def _relative(filename):
        if not isinstance(filename, (str, bytes, os.PathLike)):
            return None
        value = os.path.abspath(os.fsdecode(filename))
        return value[len(_root):].replace(os.sep, "/") if value.startswith(_root) else None

    def _write(path, payload):
        global _busy
        previous = _busy
        _busy = True
        try:
            temporary = str(path) + ".tmp"
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            os.replace(temporary, path)
        finally:
            _busy = previous

    def _start():
        path = _directory / (str(_pid) + ".start.json")
        if path.exists():
            _errors.append("duplicate PID/start (exec or PID reuse)")
        _write(path, {"version": 1, "token": _token, "pid": _pid,
                      "ppid": os.getppid(), "time_ns": time.time_ns(), "repo": _root})

    def _monitor_line(code, line):
        filename = code.co_filename
        if filename not in _filenames:
            _filenames[filename] = _relative(filename)
        name = _filenames[filename]
        if name is not None and not _busy:
            _lines.setdefault(name, set()).add(line)
        # Every process permanently belongs to one isolated test. Record each
        # location once instead of paying a Python callback on every loop.
        return _monitoring.DISABLE

    def _finish():
        global _finished
        if _finished:
            return
        _finished = True
        previous = _monitoring.register_callback(_tool_id, _monitoring.events.LINE, _monitor_line)
        installed = (previous is _monitor_line and _monitoring.get_tool(_tool_id) == _tool_name
                     and _monitoring.get_events(_tool_id) == _monitoring.events.LINE)
        _write(_directory / (str(_pid) + ".done.json"), {
            "version": 1, "token": _token, "pid": _pid, "ppid": os.getppid(),
            "repo": _root, "complete": True, "trace_installed": installed,
            "errors": _errors, "children": _children,
            "lines": {name: sorted(lines) for name, lines in _lines.items()},
            "readers": sorted(_readers),
        })

    def _after_fork():
        global _pid, _lines, _readers, _children, _errors, _busy, _finished, _spawning
        _pid = os.getpid()
        _lines, _readers, _children, _errors = {}, set(), {}, []
        _busy, _finished, _spawning = False, False, 0
        # Fork inherits disabled locations; its observations must start fresh.
        _monitoring.restart_events()
        _start()

    def _audit(event, args):
        if _busy:
            return
        if event == "open":
            mode, flags = args[1], args[2]
            if isinstance(mode, str) and "r" not in mode and "+" not in mode:
                return
            if mode is None and flags & os.O_WRONLY and not flags & os.O_RDWR:
                return
            if sys._getframe(1).f_code.co_filename == "<frozen importlib._bootstrap_external>":
                return
            name = _relative(args[0])
            if name is not None:
                _readers.add(name)
        elif event in ("os.fork", "os.posix_spawn", "subprocess.Popen"):
            if not _spawning:
                _errors.append("unwrapped process event: " + event)
        elif event in ("os.exec", "os.execve", "os.forkpty"):
            _errors.append("unsupported process transition: " + event)

    def _wrap_spawn(original, kind):
        def spawn(*args, **kwargs):
            global _spawning
            original_pid = os.getpid()
            _spawning += 1
            try:
                child = original(*args, **kwargs)
            finally:
                _spawning = _spawning - 1 if os.getpid() == original_pid else 0
            if child > 0:
                _children[str(child)] = kind
            return child
        return spawn

    # Import these before setting the trace. Their fork/exec entry points are
    # exactly those used by multiprocessing spawn and subprocess on CPython.
    import subprocess
    import _posixsubprocess

    os.fork = _wrap_spawn(os.fork, "fork")
    if hasattr(os, "posix_spawn"):
        os.posix_spawn = _wrap_spawn(os.posix_spawn, "posix_spawn")
    if hasattr(os, "posix_spawnp"):
        os.posix_spawnp = _wrap_spawn(os.posix_spawnp, "posix_spawnp")
    _posixsubprocess.fork_exec = _wrap_spawn(_posixsubprocess.fork_exec, "fork_exec")
    subprocess._fork_exec = _posixsubprocess.fork_exec
    _original_popen_init = subprocess.Popen.__init__

    def _popen_init(self, *args, **kwargs):
        global _spawning
        _spawning += 1
        try:
            _original_popen_init(self, *args, **kwargs)
        finally:
            _spawning -= 1
        _children[str(self.pid)] = "Popen"

    subprocess.Popen.__init__ = _popen_init
    _original_exit = os._exit

    def _exit(status):
        _finish()
        _original_exit(status)

    os._exit = _exit
    os.register_at_fork(after_in_child=_after_fork)
    sys.addaudithook(_audit)
    atexit.register(_finish)
    _start()
    _monitoring.use_tool_id(_tool_id, _tool_name)
    _monitoring.register_callback(_tool_id, _monitoring.events.LINE, _monitor_line)
    _monitoring.set_events(_tool_id, _monitoring.events.LINE)
