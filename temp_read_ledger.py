import json, time

try:
    with open('data/action_ledger.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception as e:
    print(f"Error loading ledger: {e}")
    data = []

cutoff = time.time() - (4 * 3600)
recent = [a for a in data if a.get('ts', 0) > cutoff]

kinds = {}
for a in recent:
    kinds[a['kind']] = kinds.get(a['kind'], 0) + 1

print(f"Total actions in last 4 hours: {len(recent)}")
for k, v in kinds.items():
    print(f"  - {k}: {v}")

if not recent:
    print("No actions in the last 4 hours.")
else:
    print("\nRecent 5 actions:")
    for a in recent[-5:]:
        print(f"[{a.get('kind')}] {a.get('uri', a.get('target_handle', ''))}")
