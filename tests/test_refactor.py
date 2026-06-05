import os
from core import config  # Added config import
from core.governance import CircuitBreaker
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
    os.environ['GROQ_API_KEY'] = 'fake_key'
    
    # Initialize with a persona string, not the API key
    client = llm.LLMClient('Test Persona')
    
    good_json = '{\n"name": "test"\n}'
    assert client.parse_json(good_json) == {'name': 'test'}
    
    # Test against realistic LLM preamble and markdown formatting
    dirty_json = 'Here is the generated output:\n{\n"name": "test"\n}'
    assert client.parse_json(dirty_json) == {'name': 'test'}
    
    bad_json = 'just plain text'
    assert client.parse_json(bad_json) == {}
    print('✅ LLMClient.parse_json passed')

def test_store_keywords():
    # Test the actual global configuration logic 
    assert hasattr(config, 'RELEVANCE_SIGNALS')
    assert hasattr(config, 'is_relevant_text')
    
    if len(config.RELEVANCE_SIGNALS) > 0:
        test_word = config.RELEVANCE_SIGNALS[0]
        assert config.is_relevant_text(f'This matches a {test_word} ok') == True
        
    print('✅ Keyword extraction passed')

if __name__ == '__main__':
    try:
        test_engagement()
        test_circuit_breaker()
        test_json_parser()
        test_store_keywords()
        print('ALL REFACTOR TESTS PASSED!')
    except Exception:
        print('TEST FAILED:')
        traceback.print_exc()
