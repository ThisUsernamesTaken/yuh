"""Replace _sniper_check with pre-fire hammer approach."""
import os
os.chdir(r"C:\Trading\btc-bias-engine")

lines = open("polymarket_copy_engine.py", "r", encoding="utf-8").readlines()

# Find _sniper_check and the old _sniper_loop
start = None
end = None
for i, line in enumerate(lines):
    if "async def _sniper_check(self)" in line:
        start = i
    if start and i > start + 5 and "    async def " in line and "_sniper" not in line:
        end = i
        break

# Also find old _sniper_loop to remove it
loop_start = None
loop_end = None
for i, line in enumerate(lines):
    if "async def _sniper_loop(self)" in line:
        loop_start = i
    if loop_start and i > loop_start + 5 and "    # " in line and "FLOW LOOP" in line:
        loop_end = i
        break

print(f"_sniper_check: {start+1} to {end}")
if loop_start:
    print(f"_sniper_loop: {loop_start+1} to {loop_end}")

# New sniper method
new_code = open("scripts/sniper_new.py", "r", encoding="utf-8").read()

# Replace sniper_check, remove sniper_loop
if loop_start and loop_end and loop_start > end:
    new_lines = lines[:start] + [new_code] + lines[end:loop_start] + lines[loop_end:]
elif end:
    new_lines = lines[:start] + [new_code] + lines[end:]
else:
    print("ERROR: couldn't find boundaries")
    exit(1)

open("polymarket_copy_engine.py", "w", encoding="utf-8").writelines(new_lines)
print("Done")
