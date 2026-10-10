import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import depth_fill

MODES = {"snapshot": depth_fill.snapshot,
         "calibrate": depth_fill.calibrate,
         "run": depth_fill.run,
         "predict": depth_fill.predict}

def pick_mode():
    if len(sys.argv) > 1:                       # python main.py calibrate
        return sys.argv[1]
    if os.environ.get("MODE"):                  # MODE=calibrate python main.py
        return os.environ["MODE"]
    if os.path.exists("mode.txt"):              # mode.txt containing "calibrate"
        return open("mode.txt").read().strip()
    return "run"                                # normal operation

mode = pick_mode().lower()
if mode not in MODES:
    sys.exit(f"Unknown mode '{mode}'. Use one of: {', '.join(MODES)}")
print(f"Starting in mode: {mode}")
MODES[mode]()