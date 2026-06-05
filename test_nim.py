import os, sys, json
sys.path.insert(0, 'src')

def load_env():
    if os.path.exists('.env'):
        for line in open('.env'):
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                os.environ[k] = v

load_env()
from clients.llm import LLMClient

try:
    client = LLMClient('You are a helpful assistant.')
    
    print('Testing FAST model...')
    r1 = client.generate('Say "fast test successful"', model_purpose='fast')
    print('FAST Output:', r1)
    
    print('\nTesting REASONING model...')
    r2 = client.generate('Say "reasoning test successful"', model_purpose='reasoning')
    print('REASONING Output:', r2)
    
    print('\nTesting VERSATILE model...')
    r3 = client.generate('Say "versatile test successful"', model_purpose='versatile')
    print('VERSATILE Output:', r3)
    
    print('\nAll endpoints successfully connected and returned data!')
except Exception as e:
    print('Error occurred:', e)
