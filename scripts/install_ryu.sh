#!/usr/bin/env bash
# Installs Ryu 4.34 into the ACTIVE Python environment (use a venv).
# The normal "pip install ryu" fails on current Python/setuptools; this script applies two small patches first.
set -euo pipefail
WORK="$(mktemp -d)"
cd "$WORK"
python3 - <<'PY'
import json, urllib.request
meta = json.load(urllib.request.urlopen("https://pypi.org/pypi/ryu/4.34/json"))
url = next(u["url"] for u in meta["urls"] if u["packagetype"] == "sdist")
urllib.request.urlretrieve(url, "ryu.tar.gz")
PY
tar xzf ryu.tar.gz
cd ryu-4.34
python3 - <<'PY'
# patch 1: newer setuptools no longer has easy_install.get_script_args
p = "ryu/hooks.py"
s = open(p).read()
s = s.replace("_main_module()._orig_get_script_args = easy_install.get_script_args",
              "_main_module()._orig_get_script_args = getattr(easy_install, 'get_script_args', None)")
s = s.replace("    easy_install.get_script_args = my_get_script_args",
              "    if _main_module()._orig_get_script_args is not None:\n        easy_install.get_script_args = my_get_script_args")
open(p, "w").write(s)
# patch 2: newer eventlet no longer has ALREADY_HANDLED
p = "ryu/app/wsgi.py"
s = open(p).read()
s = s.replace("    from eventlet.wsgi import ALREADY_HANDLED\n",
              "    try:\n        from eventlet.wsgi import ALREADY_HANDLED\n    except ImportError:\n        ALREADY_HANDLED = []\n")
open(p, "w").write(s)
PY
pip install .
cd / && rm -rf "$WORK"
ryu-manager --version
