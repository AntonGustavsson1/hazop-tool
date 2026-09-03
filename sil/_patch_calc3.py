import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\calc.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Fix the corrupted beta_d docstring
old_bd = '    def beta_d(self) -> float:\n        """' + 'Ã' + 'CCF-faktor f' + 'Ã¶' + 'r detekterade fel, typiskt ' + 'β' + '/2."""\n        return self.beta / 2.0'
new_bd = '    def beta_d(self) -> float:\n        """CCF-faktor för detekterade fel, typiskt β/2."""\n        return self.beta / 2.0'

# Let's just find and show what's there
idx = content.find('def beta_d')
print("Current beta_d section:")
print(repr(content[idx:idx+130]))
