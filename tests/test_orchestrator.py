import os
import sys

# mock env variables
os.environ["BLUESKY_HANDLE"] = "test.bsky.social"
os.environ["BLUESKY_PASSWORD"] = "password"
os.environ["GROQ_API_KEY"] = "fake"

from core import config
config.configure_logging()

from clients import adapter
class MockAdapter:
    def __init__(self, *args):
        pass
adapter.BlueskyAdapter = MockAdapter

from core.engine import FollowerEngine

def mock_sense(self):
    print("[MOCK] _sense")
    return True

def mock_run_strategist(self):
    print("[MOCK] _run_strategist")
    self.intent_queue.push({"type": "post", "priority": 10})
    self.intent_queue.push({"type": "research", "priority": 5})

def mock_original_post(self, sector, keyword):
    print(f"[MOCK] _original_post sector={sector} keyword={keyword}")

def mock_candidates_for(self, sector):
    return [], sector

FollowerEngine._sense = mock_sense
FollowerEngine._run_strategist = mock_run_strategist
FollowerEngine._original_post = mock_original_post
FollowerEngine._candidates_for = mock_candidates_for

e = FollowerEngine("test", "test")

# Mock the rates to test cascade
# Let's say post has budget, but research does not
e.rate["post"].tokens = 2.0
e.rate["research"].tokens = 0.0

print("Queue size:", len(e.intent_queue))
print("--- CYCLE 1 ---")
sleep_time = e.orchestrate()
print(f"Cycle 1 returned sleep_time: {sleep_time}")
print("Queue size:", len(e.intent_queue))

print("--- CYCLE 2 ---")
sleep_time = e.orchestrate()
print(f"Cycle 2 returned sleep_time: {sleep_time}")
print("Queue size:", len(e.intent_queue))
