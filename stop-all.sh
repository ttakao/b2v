#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec .venv/bin/python -m b2v.services stop
