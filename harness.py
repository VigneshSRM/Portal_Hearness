#!/usr/bin/env python3
"""Portable launcher for the Redmine agent harness.

AI agent skills (.agents/skills, .claude/skills) call the harness through this file:

    python harness.py <command> [options]        # same as: python -m redmine_agent <command>

Why a launcher:
* works from any folder - configuration, .env and data/ are resolved next to this file;
* uses the project's virtualenv (.venv or venv) automatically, so agents never need to
  "activate" anything and any Python 3 on PATH can start it;
* gives a clear setup hint instead of a traceback when dependencies are missing.

Only the standard library is used here, on purpose.
"""

import importlib.util
import os
import subprocess
import sys

HOME = os.path.dirname(os.path.abspath(__file__))
VENV_CANDIDATES = (
    os.path.join(".venv", "bin", "python"),
    os.path.join(".venv", "Scripts", "python.exe"),
    os.path.join("venv", "bin", "python"),
    os.path.join("venv", "Scripts", "python.exe"),
)
REQUIRED_MODULES = ("requests", "yaml", "jinja2", "fastapi", "uvicorn")


def find_venv_python():
    for rel in VENV_CANDIDATES:
        path = os.path.join(HOME, rel)
        if os.path.isfile(path):
            return path
    return None


def missing_modules():
    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


def setup_hint(missing):
    windows = os.name == "nt"
    create = "py -m venv .venv" if windows else "python3 -m venv .venv"
    pip = r".venv\Scripts\pip" if windows else ".venv/bin/pip"
    return (
        "The Redmine agent harness is not installed yet (missing: {missing}).\n"
        "Run these commands in {home}:\n"
        "    {create}\n"
        "    {pip} install -r requirements.txt\n"
        "then run your command again."
    ).format(missing=", ".join(missing), home=HOME, create=create, pip=pip)


def main():
    if sys.version_info < (3, 10) and find_venv_python() is None:
        sys.stderr.write("Python 3.10 or newer is required (found {}.{}).\n".format(*sys.version_info[:2]))
        return 4
    env = dict(os.environ)
    env.setdefault("REDMINE_AGENT_HOME", HOME)
    env["PYTHONPATH"] = HOME + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    interpreter = find_venv_python()
    if interpreter is None:
        missing = missing_modules()
        if missing:
            sys.stderr.write(setup_hint(missing) + "\n")
            return 4
        interpreter = sys.executable
    command = [interpreter, "-m", "redmine_agent"] + sys.argv[1:]
    if os.name == "nt":
        return subprocess.call(command, env=env)
    os.execve(interpreter, command, env)  # replaces this process (keeps the terminal attached)
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
