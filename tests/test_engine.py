import pytest
from core.engine import FollowerEngine

class MockRecord:
    def __init__(self, text):
        self.text = text

class MockPost:
    def __init__(self, text, cid, handle="testuser"):
        self.record = MockRecord(text)
        self.cid = cid
        class MockAuthor:
            pass
        self.author = MockAuthor()
        self.author.handle = handle

class MockLLMClientForEngine:
    def __init__(self, *args, **kwargs):
        pass

    def moderate_content(self, text, policy=None):
        if "unsafe" in text.lower():
            return {"is_safe": False}
        return {"is_safe": True}

    def parse_json(self, raw, fallback_dict=None):
        import json
        try:
            return json.loads(raw)
        except:
            return fallback_dict

    def generate_json(self, prompt, **kwargs):
        pass

class MockFollowerEngine(FollowerEngine):
    def __init__(self):
        # Override to avoid hitting actual network or full initialization
        self.llm = MockLLMClientForEngine()
        
    def _generate(self, prompt, **kwargs):
        # Mock nuanced alignment returns 'more' for everything except specific ones
        return '{"safe_cid": "more", "safe_cid_2": "keep"}'

def test_verify_posts_batch():
    engine = MockFollowerEngine()
    
    posts = [
        MockPost("This is a safe and cool post about UX.", "safe_cid"),
        MockPost("This is an UNSAFE pirate post.", "unsafe_cid"),
        MockPost("Another safe post here.", "safe_cid_2"),
    ]
    
    results = engine._verify_posts_batch(posts)
    
    # unsafe_cid should be dropped instantly by moderate_content
    assert results.get("unsafe_cid") == "drop"
    # safe_cid and safe_cid_2 should go through nuanced grading
    assert results.get("safe_cid") == "more"
    assert results.get("safe_cid_2") == "keep"

if __name__ == "__main__":
    pytest.main(["-v", "test_engine.py"])
