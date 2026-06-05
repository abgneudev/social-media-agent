import random
import config

def build_variant_prompt(sector, archetypes, length_slots, opening_slots, trends_info="", web_insights=None):
    """Construct the divergent-variants prompt."""
    slots = []
    for i, arch in enumerate(archetypes):
        guidance = config.POST_HOOK_GUIDANCE.get(arch, '').strip()
        
        # Inject curated link if chosen
        if arch == "curated_link" and web_insights and web_insights.get("curated_links"):
            link_obj = random.choice(web_insights["curated_links"])
            guidance += f" YOU MUST SHARE THIS EXACT LINK IN YOUR POST: {link_obj['url']} (Title: {link_obj['title']}). Write a sharp take on it."
            
        slots.append(
            f"[{i+1}] archetype = \"{arch}\"\n"
            f"    Archetype rule: {guidance}\n"
            f"    Length slot: {length_slots[i]}.\n"
            f"    Opening move slot: {opening_slots[i]}.\n"
            f"    If the archetype rule conflicts with the slot, follow the archetype."
        )
    slots_block = "\n\n".join(slots)
    return (
        f"You are writing THREE short Bluesky posts about '{sector}', each in a "
        f"DIFFERENT format. The three drafts must read as if written by THREE "
        f"DIFFERENT PEOPLE about the same idea, NOT three rewordings of one draft. "
        f"Follow each slot's archetype STRICTLY.\n\n"
        f"{slots_block}\n\n"
        f"{trends_info}"
        f"CRITICAL DIVERSITY: Constantly invent entirely new angles, distinct phrasing, and unexplored ideas. Do not recycle the same vocabulary or structures from typical tech posts.\n"
        f"Constraints that apply to ALL drafts: plain language, no jargon left "
        f"unexplained, no pitch, no link, no emoji, no hashtag, no em dash. Skip "
        f"parenting, body image, mental health, religion, politics, money "
        f"struggles. Explain confusing UX, design, or frontend ideas in plain "
        f"words with everyday analogies.\n\n"
        f"Respond strictly as JSON with exactly three keys per variant: 'content', 'media_type', 'media_query', and an optional 'thread_parts' array.\n"
        f"CRITICAL RULES FOR THREADS: If the archetype is 'mini_thread', you MUST provide a list of strings in 'thread_parts' (e.g. [\"part 2...\", \"part 3...\"]). The 'content' key will be the anchor post.\n"
        f"CRITICAL RULES FOR MEDIA:\n"
        f"- MAXIMIZE MEDIA USAGE: You MUST attach media to almost every post.\n"
        f"- IF media_type='gif': media_query should be a 1-3 word human emotion (e.g., 'frustrated', 'mind blown').\n"
        f"- IF media_type='image': Make an educated guess on the best visual to complement the post. The media_query MUST be highly concrete (e.g. a diagram, mockup, or code structure) and you should append a relevant industry modifier (e.g., 'dribbble', 'architecture diagram', 'figma', 'github layout') to ensure high-quality search results. If discussing an abstract theory, search for a concrete UI application of it.\n"
        f'{{"variants": [{{"content": "...", "media_type": "...", "media_query": "...", "thread_parts": []}}]}}'
    )

def build_curation_prompt(existing_lists_desc, engager_desc, velocity_desc=""):
    return (
        f"You are the curation strategist for an autonomous social media agent focused on "
        f"UX/UI design, frontend engineering, and adjacent disciplines.\n\n"
        f"YOUR EXISTING LISTS:\n{existing_lists_desc}\n\n"
        f"PEOPLE WHO RECENTLY ENGAGED WITH OUR CONTENT:\n{engager_desc}\n\n"
        f"{velocity_desc}\n\n"
        f"AVAILABLE ACTIONS:\n"
        f"1. create_list: Create a brand new curated list with a name and description\n"
        f"2. add_to_list: Add a user (by handle) to an existing list\n"
        f"3. skip: Do nothing this cycle\n\n"
        f"RULES:\n"
        f"- Only create a new list if no existing list fits the users' profiles\n"
        f"- List names should be specific and discoverable (e.g., 'Motion Design Pioneers', "
        f"'Accessibility Advocates', 'Systems Thinkers in Design')\n"
        f"- Only add users who genuinely fit a list's theme\n"
        f"- You may issue multiple actions in one response\n"
        f"- Max 3 actions per cycle\n\n"
        f'Respond strictly as JSON: {{"actions": ['
        f'{{"type": "create_list", "name": "...", "description": "..."}}, '
        f'{{"type": "add_to_list", "list_name": "...", "handle": "..."}}, '
        f'{{"type": "skip"}}]}}'
    )

def build_verify_profiles_prompt(profiles_context):
    return (
        f"You are an autonomous network analyst evaluating user profiles for strategic follows.\n"
        f"Our persona is:\n{config.PERSONA}\n\n"
        f"Evaluate the following profiles:\n{profiles_context}\n"
        f"Does each profile represent a highly credible, intellectual, or relevant practitioner "
        f"(e.g., engineer, researcher, scientist, designer) that aligns with our persona? "
        f"Reject generic tech influencers, crypto farmers, and random personal accounts.\n"
        f"Respond strictly as a JSON object mapping the handle (exact string) to a string action: 'follow', 'ignore', or 'mute'.\n"
        f"- 'follow': if they are highly credible and aligned.\n"
        f"- 'ignore': if they are irrelevant, generic, or off-topic.\n"
        f"- 'mute': if they are obvious spam, engagement farmers, crypto scammers, NSFW, or highly misaligned.\n"
        f'{{"handle1": "follow", "handle2": "ignore", "handle3": "mute"}}'
    )

def build_bio_prompt(best_sector, trends_info=""):
    return (
        f"Write a bio (max 160 chars) for {config.NAME_TEXT}. Our strongest content is "
        f"in '{best_sector}'. {trends_info}Use clear keywords for that area, "
        f"explain complex things simply, warm and approachable. "
        f"CRITICAL DIVERSITY: Find a completely fresh angle. Do not reuse the exact same phrasing as your previous bios. "
        f"Must end with 'Boston based. https://abgneudev.github.io/Portfolio/ Automated account.' No hashtags. "
        f'Respond strictly as JSON: {{"bio": "..."}}'
    )

def build_verify_posts_prompt(posts_context):
    return (
        f"You are an autonomous network analyst filtering feed content for quality.\n"
        f"Our persona is:\n{config.PERSONA}\n\n"
        f"Evaluate the following posts:\n{posts_context}\n"
        f"Does the post align with our technical rigor, or is it garbage/spam/engagement-farming/sales?\n"
        f"Respond strictly as a JSON object mapping the CID (exact string) to an action: 'more', 'keep', 'drop', 'less', or 'mute'.\n"
        f"- 'more': high quality, deeply intellectual, highly aligned to our specific persona.\n"
        f"- 'keep': relevant and acceptable, but not algorithmic-feedback worthy.\n"
        f"- 'drop': random keyword match, off-topic, sales ad, or totally irrelevant. Do not interact with it.\n"
        f"- 'less': generic tech-bro advice, bloat, highly annoying formatting.\n"
        f"- 'mute': obvious spam, engagement farmers, crypto scammers, NSFW.\n"
        f'{{"cid1": "more", "cid2": "keep", "cid3": "drop", "cid4": "less"}}'
    )

def build_sense_trends_prompt(hottest, batch):
    return (
        f"You are a Product Design Engineer (UX/UI, Frontend, OOUX, System Architecture).\n"
        f"These are recent posts in the '{hottest}' space:\n{batch}\n\n"
        f"Extract exactly 3 highly specific, trending keywords or concepts people are "
        f"actively discussing that are RELEVANT TO UX AND PRODUCT DESIGN.\n"
        f"CRITICAL RULES FOR KEYWORDS:\n"
        f"1. Must be exactly 1-3 words.\n"
        f"2. Must be highly specific tech/UX terms (e.g. 'cognitive load', 'design system', 'state management').\n"
        f"3. Must NOT be formatted as snake_case.\n"
        f"Respond strictly as JSON: "
        f'{{"keywords": ["kw1","kw2","kw3"]}}'
    )

def build_run_evolution_prompt(batch):
    return (
        f"You are an autonomous network analyst optimizing an agent's search engine.\n"
        f"The agent's persona is:\n{config.PERSONA}\n\n"
        f"These are recent posts from our timeline:\n{batch}\n\n"
        f"Your objective is to find 3 highly specific, novel search queries that expand our current niche. "
        f"Look for intersections between the persona's core focus and structural patterns in the timeline.\n\n"
        f"CRITICAL RULES FOR KEYWORDS:\n"
        f"1. LENGTH: 1 to 3 words MAXIMUM. If you generate 4 words, you fail.\n"
        f"2. FORMAT: Use normal spaces. DO NOT use snake_case, DO NOT mash words together, NO hashtags.\n"
        f"3. TARGETING: At least one keyword must explicitly target an organization, brand, or institution.\n"
        f"4. DIVERSITY: Constantly rotate institutions. Target startups, labs, and diverse brands.\n\n"
        f"Respond strictly as JSON: "
        f'{{"keywords": ["kw1", "kw2", "kw3"]}}'
    )

def build_quote_best_prompt(sector, src, hook, constraint, vision_hint):
    return (
        f"This post is about '{sector}':\n\"{src}\"\n\n"
        f"Write one short comment (max 200 chars) to quote-post it, adding a "
        f"genuinely useful plain-language insight that builds on it. Use a "
        f"'{hook}' angle. {config.POST_HOOK_GUIDANCE.get(hook,'')} Never pitch anything. "
        f"{constraint}"
        f"{vision_hint}"
        f'Respond strictly as JSON: {{"comment": "..."}}'
    )

def build_helpful_reply_prompt(sector, batch, hook, whale_constraint, vision_hint):
    return (
        f"These are live posts about '{sector}':\n\n{batch}\n"
        f"Pick the SINGLE post where a short, kind, helpful reply would make the "
        f"person feel heard and less stuck. Add real value: a clearer way to think "
        f"about their problem, a small concrete tip, or a good question. Use a "
        f"'{hook}' angle. {config.REPLY_HOOK_GUIDANCE.get(hook,'')} Explain any technical "
        f"idea in plain words with an everyday analogy. If the post is sensitive "
        f"or heavily polarized, respond with action='skip'. {whale_constraint}"
        f"{vision_hint}"
        f'Respond strictly as JSON: {{"index": 0, "reply": "...", "action": "reply"}}'
    )

def build_profile_optimization_prompt(best_sector, bio_context):
    return (
        f"You are an elite Brand Strategist optimizing the profile of an autonomous AI agent.\n"
        f"The agent's core identity (which you must retain) is:\n{config.PERSONA}\n\n"
        f"CRITICAL RULES:\n"
        f"1. You MUST include any Call To Actions (CTAs), website links, contact emails, or secondary account handles from the core identity in the new bio.\n"
        f"2. DO NOT change the agent's core identity, persona, beliefs, or mission to match trending topics. You are ONLY borrowing the structural formatting (e.g., bullet points, conciseness, punctuation style) of the credible creators, NOT their actual content or job titles.\n"
        f"3. Strict Character Limits: Display Name must be under 50 characters. Bio must be under 250 characters.\n\n"
        f"The agent's most successful topic is: '{best_sector}'.\n"
        f"Here are the bios of 5 highly credible creators in this exact space:\n{bio_context}\n\n"
        f"Respond STRICTLY as JSON:\n"
        f"{{\n  \"display_name\": \"...\",\n  \"bio\": \"...\"\n}}"
    )
