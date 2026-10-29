"""Phase 2 offline demo (FakeLLM, no API key, no cost).

Run from the repo root after `. .\\scripts\\dev-env.ps1`:
    & $env:DEVPILOT_PYTHON backend\\scripts\\demo_calculator.py
"""

from app.demo.calculator import run_demo

if __name__ == "__main__":
    run_demo()
