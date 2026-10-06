#!/usr/bin/env sh
# Lance l'application : crée l'environnement au premier passage, puis ouvre localhost:5000
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python app.py
