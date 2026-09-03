import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil")

# Test that sil.py imports work
try:
    import sil
    print("sil.py imported OK")
    # Check that pfd_all_architectures is importable
    from calc import pfd_all_architectures
    print("pfd_all_architectures imported OK")
    import verification_db as vdb
    print("verification_db imported OK")
    print("All imports successful")
except Exception as e:
    print(f"Import error: {e}")
    import traceback; traceback.print_exc()
