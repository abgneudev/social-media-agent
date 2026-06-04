import klipy
import utils
import llm
import store
from governance import CircuitBreaker, RateBudget
from atproto import exceptions
import traceback

class DummyPost:
    def __init__(self, l, r, q, c):
        self.like_count = l
        self.repost_count = r
        self.quote_count = q
        self.reply_count = c

def test_engagement():
    p = DummyPost(10, 5, 2, 1)
    eng = utils.get_total_engagement(p)
    assert eng == 18, f'Expected 18, got {eng}'
    print('✅ get_total_engagement passed')

def test_circuit_breaker():
    breaker = CircuitBreaker()
    
    # 1. Success case
    with breaker.guard('test_success'):
        pass
    
    # 2. Failure case
    try:
        with breaker.guard('test_fail'):
            raise exceptions.AtProtocolError('fake error')
    except exceptions.AtProtocolError:
        pass
        
    assert breaker.consecutive_failures == 1, f'Expected 1, got {breaker.consecutive_failures}'
    
    # 3. Non-ATProto exceptions should not trip the breaker
    try:
        with breaker.guard('test_value_error'):
            raise ValueError('should bubble up, not record failure')
    except ValueError:
        pass
        
    assert breaker.consecutive_failures == 1, f'Expected 1, got {breaker.consecutive_failures} (should not increment)'
    print('✅ CircuitBreaker.guard passed')

def test_json_parser():
    import os
    os.environ['GROQ_API_KEY'] = 'fake_key'
    client = llm.LLMClient('fake_key')
    
    good_json = '{\n"name": "test"\n}'
    assert client.parse_json(good_json) == {'name': 'test'}
    
    dirty_json = 'Here is the result:\n```json\n{"foo": "bar"}\n```'
    assert client.parse_json(dirty_json) == {'foo': 'bar'}
    
    bad_json = 'just plain text'
    assert client.parse_json(bad_json) == {}
    print('✅ llm.parse_json passed')

def test_store_keywords():
    s = store.Store()
    assert hasattr(s, 'keyword_map')
    assert hasattr(s, 'relevance_signals')
    assert hasattr(s, 'relevance_re')
    
    # Ensure compiling worked
    assert s.is_relevant_text('This matches a ' + s.relevance_signals[0] + ' ok') == True
    print('✅ Store keyword extraction passed')

if __name__ == '__main__':
    try:
        test_engagement()
        test_circuit_breaker()
        test_json_parser()
        test_store_keywords()
        print('ALL REFACTOR TESTS PASSED!')
    except Exception as e:
        print('TEST FAILED:')
        traceback.print_exc()
