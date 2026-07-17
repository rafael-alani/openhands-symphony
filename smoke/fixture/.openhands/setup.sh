#!/bin/sh
set -eu
python3 -m compileall -q calculator.py tests
