import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\sil.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Fix the broken tooltip string (has literal newline instead of \n)
# Find the broken line
bad = '_Tip(tk.Label(fit_frm, text="(?)", font=FONT_S, fg="#aaa"),\n             "Ange λDU och λDD direkt i FIT (fel per 10⁹ h) från FMEDA-data.\n"\n             "Beräknar λD = (λDU+λDD)×10⁻⁹ och DC = λDD/(λDU+λDD).").pack(side="left")'
good = '_Tip(tk.Label(fit_frm, text="(?)", font=FONT_S, fg="#aaa"),\n             "Ange λDU och λDD direkt i FIT (fel per 10⁹ h) från FMEDA-data.\\n"\n             "Beräknar λD = (λDU+λDD)×10⁻⁹ och DC = λDD/(λDU+λDD).").pack(side="left")'

if bad in content:
    content = content.replace(bad, good)
    print("Fixed tooltip newline OK")
else:
    # Try to find it differently
    idx = content.find('_Tip(tk.Label(fit_frm')
    if idx >= 0:
        print("Found at:", idx)
        snippet = content[idx:idx+250]
        print(repr(snippet))
    else:
        print("Not found at all")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Saved")
