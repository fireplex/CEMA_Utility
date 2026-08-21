import re

with open('cema_app.py', 'r', encoding='utf-8') as f:
    d = f.read()

# Strip emojis
d = re.sub(r'[^\x00-\x7F]+', '', d)

# Fix sizing policy
d = d.replace(
    'control_group = QGroupBox("SDR Parameters")',
    'from PyQt6.QtWidgets import QSizePolicy\n        control_group = QGroupBox("SDR Parameters")\n        control_group.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)'
)

with open('cema_app.py', 'w', encoding='utf-8') as f:
    f.write(d)
print("Cleaned!")
