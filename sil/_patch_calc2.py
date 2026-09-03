import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\calc.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

idx = content.find('def beta_d')
if idx >= 0:
    print("beta_d found at", idx)
    # show ascii safe
    snippet = content[idx:idx+120].encode('ascii','replace').decode()
    print(snippet)
else:
    print("beta_d NOT found")

idx2 = content.find('def pfd_simplified')
if idx2 >= 0:
    print("pfd_simplified found at", idx2)
