import sys
import os
os.chdir(r"E:\ABU\360Car\app")
sys.path.insert(0, r"E:\ABU\360Car\app")

try:
    import main
    print("MAIN IMPORTED OK")
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
