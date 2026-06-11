#!/usr/bin/env bash
cd "$(dirname "$0")"
mkdir -p data/inputs data/outputs data/tmp data/logs
python3 -c "import requests" >/dev/null 2>&1 || python3 -m pip install --user --upgrade requests websocket-client
python3 local_server.py
