import logging
from core import config
config.configure_logging(logging.DEBUG)

from intelligence import memory

if __name__ == "__main__":
    print("Writing memory...")
    memory.remember_interaction(
        user_handle="design_guru",
        user_text="Your posts about UX are really vague.",
        agent_reply="I appreciate the feedback! I'll try to provide more concrete UI teardowns."
    )

    print("\nQuerying history for @design_guru...")
    history = memory.recall_history("design_guru", "Do you have any examples of bad UX?")
    print(f"History returned:\n{history}")
    assert "Your posts about UX" in history, "Failed to retrieve history for design_guru!"

    print("\nQuerying history for someone else...")
    history2 = memory.recall_history("random_user", "Do you have any examples of bad UX?")
    print(f"History returned:\n{history2}")
    assert history2 == "", "Returned history for wrong user!"
    
    print("\nMemory tests passed!")
