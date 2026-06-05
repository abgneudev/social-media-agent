import logging
logging.basicConfig(level=logging.INFO)

from core.config import KEYWORD_MAP, RELEVANCE_SIGNALS
from core.engine import FollowerEngine

engine = FollowerEngine("dummy", "dummy")
print("KEYWORD MAP:")
for k, v in KEYWORD_MAP.items():
    print(f"  {k}: {len(v)} keywords")
print(f"RELEVANCE_SIGNALS: {len(RELEVANCE_SIGNALS)} signals")
