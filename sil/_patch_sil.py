import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\sil.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Step 1: Update import to add pfd_all_architectures
old_import = '''from calc import (
    Architecture, ComponentParams, SubsystemParams,
    calc_sif, calc_subsystem, SIFResult, SIL_LIMITS,
    sil_from_pfd, validate_component, pfd_simplified,
)'''

new_import = '''from calc import (
    Architecture, ComponentParams, SubsystemParams,
    calc_sif, calc_subsystem, SIFResult, SIL_LIMITS,
    sil_from_pfd, validate_component, pfd_simplified, pfd_all_architectures,
)'''

if old_import in content:
    content = content.replace(old_import, new_import)
    print("Step 1 (import) OK")
else:
    print("Step 1 FAILED")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Saved step 1")
