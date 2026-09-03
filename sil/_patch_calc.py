import sys

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\calc.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Step 1: Add FIT fields and update lambda_du/lambda_dd properties
old1 = '    ccf_model: str    = "beta"  # "beta" eller "mooNbeta"\n\n    @property\n    def lambda_du(self) -> float:\n        return self.lambda_d * (1.0 - self.dc)\n\n    @property\n    def lambda_dd(self) -> float:\n        return self.lambda_d * self.dc'

new1 = '    ccf_model: str    = "beta"  # "beta" eller "mooNbeta"\n    # FIT-ingång: när > 0 åsidosätter lambda_d*(1-dc) / lambda_d*dc\n    lambda_du_fit: float = 0.0  # λDU i FIT (fel per 10⁹ h)\n    lambda_dd_fit: float = 0.0  # λDD i FIT (fel per 10⁹ h)\n\n    @property\n    def lambda_du(self) -> float:\n        if self.lambda_du_fit > 0:\n            return self.lambda_du_fit * 1e-9\n        return self.lambda_d * (1.0 - self.dc)\n\n    @property\n    def lambda_dd(self) -> float:\n        if self.lambda_dd_fit > 0:\n            return self.lambda_dd_fit * 1e-9\n        return self.lambda_d * self.dc\n\n    @property\n    def beta_d(self) -> float:\n        """Ã•CCF-faktor fÃ¶r detekterade fel, typiskt β/2."""\n        return self.beta / 2.0'

if old1 in content:
    content = content.replace(old1, new1)
    print("Step 1 OK")
else:
    print("Step 1 FAILED")
    sys.exit(1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Saved step 1")
