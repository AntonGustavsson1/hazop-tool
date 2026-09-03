import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\sil.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Step 5: Update to_dict to include fit_du and fit_dd
old_to_dict = '''    def to_dict(self) -> dict:
        d = {"name":self.v_name.get(),"arch":self.v_arch.get(),
             "comp_type":self.v_ctype.get(),"ccf_model":self.v_ccf.get(),
             "calc_method":self.v_method.get(),
             "channels":[dict(ch) for ch in self._channels],
             **{k:v.get() for k,v in self._vars.items()}}
        return d'''

new_to_dict = '''    def to_dict(self) -> dict:
        d = {"name":self.v_name.get(),"arch":self.v_arch.get(),
             "comp_type":self.v_ctype.get(),"ccf_model":self.v_ccf.get(),
             "calc_method":self.v_method.get(),
             "fit_du":self.v_fit_du.get(),"fit_dd":self.v_fit_dd.get(),
             "channels":[dict(ch) for ch in self._channels],
             **{k:v.get() for k,v in self._vars.items()}}
        return d'''

if old_to_dict in content:
    content = content.replace(old_to_dict, new_to_dict)
    print("Step 5 (to_dict) OK")
else:
    print("Step 5 FAILED")

# Step 6: Update from_dict to restore fit_du and fit_dd
old_from_dict = '''    def from_dict(self, d: dict):
        self.v_name.set(d.get("name",""))
        self.v_arch.set(d.get("arch","1oo1"))
        self.v_ctype.set(d.get("comp_type","A (enkel)"))
        self.v_ccf.set(d.get("ccf_model","beta"))
        self.v_method.set(d.get("calc_method","markov"))
        self._channels = [dict(ch) for ch in d.get("channels",[])]
        for k,v in self._vars.items():
            if k in d: v.set(d[k])'''

new_from_dict = '''    def from_dict(self, d: dict):
        self.v_name.set(d.get("name",""))
        self.v_arch.set(d.get("arch","1oo1"))
        self.v_ctype.set(d.get("comp_type","A (enkel)"))
        self.v_ccf.set(d.get("ccf_model","beta"))
        self.v_method.set(d.get("calc_method","markov"))
        self.v_fit_du.set(d.get("fit_du",""))
        self.v_fit_dd.set(d.get("fit_dd",""))
        self._channels = [dict(ch) for ch in d.get("channels",[])]
        for k,v in self._vars.items():
            if k in d: v.set(d[k])'''

if old_from_dict in content:
    content = content.replace(old_from_dict, new_from_dict)
    print("Step 6 (from_dict) OK")
else:
    print("Step 6 FAILED")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Saved steps 5-6")
