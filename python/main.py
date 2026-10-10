import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import depth_fill

# App Lab starts main.py without arguments, so choose the mode here:
#   "snapshot"  -> save calib_frame.jpg to draw the masks on
#   "calibrate" -> with the bin EMPTY, build the empty-bin depth map
#   "run"       -> normal operation: measure every 15 minutes and log
MODE = "snapshot"

{"snapshot": depth_fill.snapshot,
 "calibrate": depth_fill.calibrate,
 "run": depth_fill.run,
 "predict": depth_fill.predict}[MODE]()