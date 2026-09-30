import re
import subprocess
import xml.etree.ElementTree as ET

ADB = ['adb', '-s', '0000000000000000']
subprocess.run(ADB + ['shell', 'uiautomator', 'dump', '/data/local/tmp/codex-ui.xml'], capture_output=True, text=True, timeout=25, check=True)
xml = subprocess.run(ADB + ['shell', 'cat', '/data/local/tmp/codex-ui.xml'], capture_output=True, text=True, timeout=10, check=True).stdout
root = ET.fromstring(xml)
if any('keyguard_pin_view' in n.get('resource-id', '') for n in root.iter('node')):
    print('PIN lock screen: user must unlock phone.')
else:
    for node in root.iter('node'):
        text = node.get('text', '') or node.get('content-desc', '')
        if text:
            text = re.sub(r'[\w.+-]+@[\w.-]+', '[account address]', text)
            print(text, node.get('resource-id'), node.get('bounds'))
