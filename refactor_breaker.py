import re

with open('c:/flutter/social-agent/engine.py', 'r', encoding='utf-8') as f:
    code = f.read()

pattern = re.compile(
    r'(\s+)try:\n\s+(self\.net\.[a-zA-Z_]+\(.*?)\n\s+self\.breaker\.record_success\(\)\n\s+except exceptions\.AtProtocolError as e:\n\s+logger\.warning\(f?[\"\'].*?[\"\']\)\n\s+self\.breaker\.record_failure\(\)\n\s+return\s*\n',
    re.DOTALL
)

def repl(m):
    indent = m.group(1)
    call = m.group(2)
    return f'{indent}with self.breaker.guard():\n{indent}    {call}\n{indent}    return\n'

new_code, count = pattern.subn(repl, code)

pattern2 = re.compile(
    r'(\s+)try:\n\s+(self\.net\.[a-zA-Z_]+\(.*?)\n\s+self\.breaker\.record_success\(\)\n\s+except exceptions\.AtProtocolError as e:\n\s+logger\.warning\(f?[\"\'].*?[\"\']\)\n\s+self\.breaker\.record_failure\(\)\n',
    re.DOTALL
)

def repl2(m):
    indent = m.group(1)
    call = m.group(2)
    return f'{indent}with self.breaker.guard():\n{indent}    {call}\n'

new_code, count2 = pattern2.subn(repl2, new_code)

pattern3 = re.compile(
    r'(\s+)try:\n\s+(intent_id, uri = self\._publish_with_reconcile\(.*?)\n\s+except exceptions\.AtProtocolError as e:\n\s+logger\.warning\(f?[\"\'].*?[\"\']\)\n\s+self\.breaker\.record_failure\(\)\n\s+return\n\s+self\.breaker\.record_success\(\)\n',
    re.DOTALL
)
def repl3(m):
    indent = m.group(1)
    call = m.group(2)
    return f'{indent}with self.breaker.guard():\n{indent}    {call}\n{indent}    return\n'
new_code, count3 = pattern3.subn(repl3, new_code)

with open('c:/flutter/social-agent/engine.py', 'w', encoding='utf-8') as f:
    f.write(new_code)
print(f'Replaced {count + count2 + count3} breaker blocks')
