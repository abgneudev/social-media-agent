
import re

with open('c:/flutter/social-agent/engine.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace config.KEYWORD_MAP
code = code.replace('config.KEYWORD_MAP', 'self.store.keyword_map')

# Replace KEYWORD_MAP (if imported directly)
code = code.replace('KEYWORD_MAP', 'self.store.keyword_map')

# Replace config.RELEVANCE_RE and RELEVANCE_RE
code = code.replace('config.RELEVANCE_RE', 'self.store.relevance_re')
code = code.replace('RELEVANCE_RE', 'self.store.relevance_re')

# The imports might now look like self.store.keyword_map, self.store.relevance_re, which is invalid syntax
# But wait, python script can just use simple string replacement and we fix the imports manually.
code = re.sub(r'from config import \(.*?\)', lambda m: m.group(0).replace('self.store.keyword_map,', '').replace('self.store.relevance_re,', ''), code, flags=re.DOTALL)
# also if they are on line 26:
code = code.replace('    self.store.keyword_map, self.store.relevance_re,\n', '')

with open('c:/flutter/social-agent/engine.py', 'w', encoding='utf-8') as f:
    f.write(code)
print('Replaced KEYWORD_MAP and RELEVANCE_RE with store attributes')

