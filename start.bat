@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
python d2r_xp_pulse_ui.py
