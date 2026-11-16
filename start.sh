#!/bin/zsh
# Startet den virtuellen Voice-Incubator-Satelliten (legt venv beim ersten Mal an)
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  uv venv -q -p 3.12 .venv && uv pip install -q -p .venv -r requirements.txt
fi
exec .venv/bin/python satellite.py "$@"
