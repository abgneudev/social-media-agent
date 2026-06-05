import os
import re
from pathlib import Path

# Mapping of module names to their new package paths
file_map = {
    'engine': 'core',
    'store': 'core',
    'config': 'core',
    'governance': 'core',
    'strategy': 'intelligence',
    'prompts': 'intelligence',
    'meta_critic': 'intelligence',
    'web_research': 'intelligence',
    'analyzer': 'intelligence',
    'memory': 'intelligence',
    'adapter': 'clients',
    'llm': 'clients',
    'serper': 'clients',
    'utils': 'utils',
    'warden': 'utils',
    'firehose_daemon': 'daemons'
}

def process_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    original_content = content

    for mod, category in file_map.items():
        # Match `import X` -> `from category import X`
        # We only match `import X` at the start of a line or after spaces, followed by a newline or comment
        # It handles `import llm` or `import llm as xyz`
        content = re.sub(
            rf'^([ \t]*)import {mod}(\b)', 
            rf'\1from {category} import {mod}\2', 
            content, 
            flags=re.MULTILINE
        )

        # Match `from X import ...` -> `from category.X import ...`
        content = re.sub(
            rf'^([ \t]*)from {mod} import ', 
            rf'\1from {category}.{mod} import ', 
            content, 
            flags=re.MULTILINE
        )

    if content != original_content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Updated {filepath}")

# Process src folder
src_dir = Path("src")
for root, dirs, files in os.walk(src_dir):
    for file in files:
        if file.endswith('.py'):
            process_file(os.path.join(root, file))

# Process tests folder
tests_dir = Path("tests")
for root, dirs, files in os.walk(tests_dir):
    for file in files:
        if file.endswith('.py'):
            process_file(os.path.join(root, file))

# Process entry points
process_file("run.py")
process_file("klipy.py")

print("Done refactoring imports.")
