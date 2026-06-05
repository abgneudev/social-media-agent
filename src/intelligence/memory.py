"""Epistemic & Episodic Memory using ChromaDB

This module allows the agent to semantically search its own memory banks,
giving it the ability to remember past interactions with specific users.
"""
import time
import chromadb
from core.config import logger, STATE_DIR

# Initialize ChromaDB persistent client locally
# We store the vector database inside the state directory
CHROMA_DIR = STATE_DIR / "chroma_db"
try:
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    
    # 1. Interactions (Episodic memory with users)
    collection_interactions = client.get_or_create_collection(name="interactions")
    
    # 2. Self Threads (Narrative Arc / Anti-Repetition)
    collection_self_threads = client.get_or_create_collection(name="self_threads")
    
    # 3. Swipe File (Viral Structure Memory)
    collection_swipe = client.get_or_create_collection(name="swipe_file")
    
    # 4. Knowledge Base (Authoritative Memory)
    collection_knowledge = client.get_or_create_collection(name="knowledge_base")
    
except Exception as e:
    logger.error(f"[MEMORY] Failed to initialize ChromaDB: {e}")
    collection_interactions = None
    collection_self_threads = None
    collection_swipe = None
    collection_knowledge = None

def remember_interaction(user_handle: str, user_text: str, agent_reply: str):
    """
    Saves an interaction to the vector database.
    """
    if not collection_interactions:
        return
        
    try:
        # Create a combined semantic string for embedding
        document = f"User @{user_handle} said: '{user_text}'. We replied: '{agent_reply}'"
        
        # Unique ID for the interaction
        interaction_id = f"{user_handle}_{time.time()}"
        
        # Save to Chroma
        collection_interactions.add(
            documents=[document],
            metadatas=[{"handle": user_handle, "timestamp": time.time()}],
            ids=[interaction_id]
        )
        logger.info(f"   [MEMORY] Successfully embedded interaction with @{user_handle}")
    except Exception as e:
        logger.warning(f"   [MEMORY] Failed to store interaction: {e}")

def recall_history(user_handle: str, current_query: str, limit: int = 3) -> str:
    """
    Queries the vector database for past interactions with a specific user
    that are semantically related to the current query.
    Returns a formatted string of the history, or an empty string if none found.
    """
    if not collection_interactions:
        return ""
        
    try:
        results = collection_interactions.query(
            query_texts=[current_query],
            n_results=limit,
            where={"handle": user_handle}
        )
        
        if not results or not results.get("documents") or not results["documents"][0]:
            return ""
            
        docs = results["documents"][0]
        if not docs:
            return ""
            
        history = "\n".join([f"- {doc}" for doc in docs])
        logger.info(f"   [MEMORY] Recalled {len(docs)} past interactions with @{user_handle}")
        return history
    except Exception as e:
        logger.warning(f"   [MEMORY] Failed to recall history: {e}")
        return ""

def remember_self_thread(sector: str, text: str):
    if not collection_self_threads:
        return
    try:
        interaction_id = f"self_{time.time()}"
        collection_self_threads.add(
            documents=[text],
            metadatas=[{"sector": sector, "timestamp": time.time()}],
            ids=[interaction_id]
        )
        logger.info(f"   [MEMORY] Saved self thread for sector '{sector}'")
    except Exception as e:
        logger.warning(f"   [MEMORY] Failed to store self thread: {e}")

def recall_self_threads(sector: str, limit: int = 2) -> str:
    if not collection_self_threads:
        return ""
    try:
        results = collection_self_threads.query(
            query_texts=[sector],
            n_results=limit,
            where={"sector": sector}
        )
        if not results or not results.get("documents") or not results["documents"][0]:
            return ""
        docs = results["documents"][0]
        return "\n".join([f"- {doc}" for doc in docs])
    except Exception:
        return ""

def save_to_swipe_file(text: str, engagement: float):
    if not collection_swipe:
        return
    try:
        interaction_id = f"swipe_{time.time()}"
        collection_swipe.add(
            documents=[text],
            metadatas=[{"engagement": engagement, "timestamp": time.time()}],
            ids=[interaction_id]
        )
        logger.info(f"   [MEMORY] Saved viral post to swipe file (eng: {engagement})")
    except Exception as e:
        logger.warning(f"   [MEMORY] Failed to store swipe file: {e}")

def recall_swipe_file(limit: int = 1) -> str:
    if not collection_swipe:
        return ""
    try:
        # Since we just want high engagement structures, we can query for "highly engaging viral format"
        results = collection_swipe.query(
            query_texts=["highly engaging viral format listicle short hook"],
            n_results=limit
        )
        if not results or not results.get("documents") or not results["documents"][0]:
            return ""
        docs = results["documents"][0]
        return "\n".join([f"- {doc}" for doc in docs])
    except Exception:
        return ""

def save_knowledge(topic: str, fact: str):
    if not collection_knowledge:
        return
    try:
        interaction_id = f"fact_{time.time()}"
        collection_knowledge.add(
            documents=[fact],
            metadatas=[{"topic": topic, "timestamp": time.time()}],
            ids=[interaction_id]
        )
        logger.info(f"   [MEMORY] Saved fact to knowledge base: '{topic}'")
    except Exception as e:
        logger.warning(f"   [MEMORY] Failed to store knowledge: {e}")

def recall_knowledge(topic: str, limit: int = 20, max_distance: float = 1.5) -> str:
    if not collection_knowledge:
        return ""
    try:
        results = collection_knowledge.query(
            query_texts=[topic],
            n_results=limit
        )
        if not results or not results.get("documents") or not results["documents"][0]:
            return ""
            
        docs = results["documents"][0]
        distances = results.get("distances", [[0] * len(docs)])[0]
        
        filtered_docs = []
        for doc, dist in zip(docs, distances):
            # L2 distance threshold filter. Lower is more similar.
            if dist <= max_distance:
                filtered_docs.append(doc)
                
        if not filtered_docs:
            return ""
            
        return "\n".join([f"- {doc}" for doc in filtered_docs])
    except Exception as e:
        logger.error(f"[MEMORY] recall_knowledge failed: {e}")
        return ""
