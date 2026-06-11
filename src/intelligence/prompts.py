import random

def build_variant_prompt(soul, sector, archetypes, length_slots, opening_slots, trends_info="", web_insights=None, learned_signals=None):
    """Construct the divergent-variants prompt."""
    slots = []
    for i, arch in enumerate(archetypes):
        guidance = soul.post_hook_guidance.get(arch, '').strip()
        
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

    signals_text = ""
    if learned_signals:
        top_signals = ", ".join(learned_signals[-30:])
        signals_text = f"LEARNED HIGH-VALUE SIGNALS: These are the exact topics, vocabulary, and concepts that elite professionals in your niche use: [{top_signals}]. Naturally weave these into your posts to attract the right audience. Do NOT force them in unnaturally.\n\n"

    return (
        f"You are an expert, highly insightful network agent drafting original anchor posts.\n"
        f"Your core persona and identity is:\n{soul.persona}\n\n"
        f"{signals_text}"
        f"You are writing THREE short Bluesky posts about '{sector}', each in a "
        f"DIFFERENT format. The three drafts must read as if written by THREE "
        f"DIFFERENT PEOPLE about the same idea, NOT three rewordings of one draft. "
        f"Follow each slot's archetype STRICTLY.\n\n"
        f"{slots_block}\n\n"
        f"{trends_info}"
        f"CRITICAL DIVERSITY: Constantly invent entirely new angles, distinct phrasing, and unexplored ideas. Do not recycle the same vocabulary or structures from typical tech posts.\n"
        f"CRITICAL CONSTRAINT: DO NOT start your posts with repetitive rhetorical questions like \"Why are we...\", \"How does...\", or \"Why is...\". Make statements, offer insights, or state contrarian facts instead of asking rhetorical questions.\n"
        f"Constraints that apply to ALL drafts: plain language, no jargon left "
        f"unexplained, no pitch, no emoji, no hashtag, no em dash. Skip "
        f"parenting, body image, mental health, religion, politics, money "
        f"struggles, sexual content, and NSFW topics. Explain confusing or complex ideas in plain "
        f"words with everyday analogies. If sharing a link from your knowledge base, do so naturally.\n\n"
        f"Respond strictly as JSON with exactly three keys per variant: 'content', 'media_type', 'media_query', and an optional 'thread_parts' array.\n"
        f"CRITICAL RULES FOR THREADS: If the archetype is 'mini_thread', you MUST provide a list of strings in 'thread_parts' (e.g. [\"part 2...\", \"part 3...\"]). The 'content' key will be the anchor post.\n"
        f"CRITICAL RULES FOR MEDIA:\n"
        f"- MAXIMIZE MEDIA USAGE: You MUST attach media to almost every post.\n"
        f"- IF media_type='gif': media_query should be a 1-3 word human emotion (e.g., 'frustrated', 'mind blown').\n"
        f"- IF media_type='image': Make an educated guess on the best visual to complement the post. The media_query MUST be highly concrete (e.g. a mockup, interaction flow, design system example). Append a relevant industry modifier to ensure high-quality design results: 'figma prototype', 'dribbble shot', 'interaction design', 'UI mockup', 'design system diagram'. If discussing an abstract theory, search for a concrete UI application of it.\n"
        f'{{"variants": [{{"content": "...", "media_type": "...", "media_query": "...", "thread_parts": []}}]}}'
    )

def build_curation_prompt(soul, existing_lists_desc, engager_desc, velocity_desc=""):
    return (
        f"You are the curation strategist for an autonomous social media agent.\n"
        f"The agent's persona is:\n{soul.persona}\n\n"
        f"YOUR EXISTING LISTS:\n{existing_lists_desc}\n\n"
        f"PEOPLE WHO RECENTLY ENGAGED WITH OUR CONTENT:\n{engager_desc}\n\n"
        f"{velocity_desc}\n\n"
        f"AVAILABLE ACTIONS:\n"
        f"1. create_list: Create a brand new curated list with a name and description\n"
        f"2. add_to_list: Add a user (by handle) to an existing list\n"
        f"3. skip: Do nothing this cycle\n\n"
        f"RULES:\n"
        f"- Only create a new list if no existing list fits the users' profiles\n"
        f"- List names should be specific to the agent's domain (e.g., 'Top Tier Experts', "
        f"'Accessibility Advocates', 'Systems Thinkers in Design')\n"
        f"- Only add users who genuinely fit a list's theme\n"
        f"- You may issue multiple actions in one response\n"
        f"- Max 3 actions per cycle\n\n"
        f'Respond strictly as JSON: {{"actions": ['
        f'{{"type": "create_list", "name": "...", "description": "..."}}, '
        f'{{"type": "add_to_list", "list_name": "...", "handle": "..."}}, '
        f'{{"type": "skip"}}]}}'
    )

def build_verify_profiles_prompt(soul, profiles_context, learned_signals=None):
    signals_text = ""
    if learned_signals:
        top_signals = ", ".join(learned_signals[-30:])
        signals_text = f"LEARNED HIGH-VALUE SIGNALS: These terms are strong indicators of design/UX professionals that match our persona: [{top_signals}]. Use them as positive criteria.\n\n"

    return (
        f"You are an autonomous network analyst evaluating user profiles for strategic follows.\n"
        f"OUR PERSONA (design‑dominant, code as supplement):\n{soul.persona}\n\n"
        f"{signals_text}"
        f"Evaluate the following profiles:\n{profiles_context}\n"
        f"GUIDANCE: Determine whether each profile represents a credible practitioner whose primary focus is design (UX, product design, design systems, accessibility, HCI, design engineering) and aligns with our persona. We do NOT follow pure developers, even if they are technically impressive. Look for evidence in the bio, display name, and description that design is the core discipline.\n"
        f"MINDSET: Filter out spam, but also be strict about domain alignment. It's better to mark a batch entirely as 'ignore' than to follow the wrong people.\n"
        f"Respond strictly as a JSON object mapping the handle (exact string) to a string action: 'follow', 'ignore', or 'mute'.\n"
        f"- 'follow': design‑focused profile that fits our persona.\n"
        f"- 'ignore': irrelevant, off‑topic, or ambiguous.\n"
        f"- 'mute': spam, bots, crypto, NSFW, or highly misaligned.\n"
        f"CRITICAL: The keys in your JSON MUST be the exact handle strings from the profiles above (e.g., 'johndoe.bsky.social'). Do not use placeholders like 'handle1'.\n"
        f'Example Output: {{"uxdesigner.bsky.social": "follow", "reactdev.bsky.social": "ignore", "spambot.bsky.social": "mute"}}'
    )

def build_bio_prompt(soul, best_sector, trends_info=""):
    return (
        f"You are updating the Bluesky profile of a Product Design Engineer.\n"
        f"Their core identity is:\n{soul.persona}\n\n"
        f"Write a bio (max 160 characters) for this account. Our most successful "
        f"content lives in the sector '{best_sector}', so the bio should naturally "
        f"include one or two keywords from that area.\n\n"
        f"Voice rules:\n"
        f"- Plain, everyday language. No design jargon, no engineering buzzwords.\n"
        f"- Warm, approachable, and human. Sound like a real person describing what they care about.\n"
        f"- Show the design‑first mindset: lead with how you think about products, not what tools you use.\n"
        f"- Do NOT say 'Automated account.' or anything that breaks the human voice.\n\n"
        f"{trends_info}\n"
        f"CRITICAL DIVERSITY: Invent a completely fresh angle. Never reuse phrasing "
        f"from previous bios. The bio must end with the exact string: "
        f"'Boston based. https://abgneudev.github.io/Portfolio/' No hashtags, no emoji.\n\n"
        f'Respond strictly as JSON: {{"bio": "..."}}'
    )

def build_verify_posts_prompt(soul, posts_context, learned_signals=None):
    signals_text = ""
    if learned_signals:
        top_signals = ", ".join(learned_signals[-30:])
        signals_text = f"LEARNED HIGH-VALUE SIGNALS: You have previously identified these keywords as strong indicators of top 20% elite professionals: [{top_signals}]. Use these as positive criteria to find high-value targets.\n\n"

    return (
        f"You are an autonomous network analyst filtering feed content for quality.\n"
        f"Our persona is:\n{soul.persona}\n\n"
        f"{signals_text}"
        f"Evaluate the following posts AND their authors:\n{posts_context}\n"
        f"Does the post align with our design depth and craft, and is the author a credible professional?"
        f"MINDSET: You must filter out spam, but you also MUST find the best posts in this batch to interact with. If you drop everything, the agent fails.\n"
                f"CRITICAL DOMAIN FILTER: You must **only** grade as 'keep' or 'more' posts that clearly discuss UX design, interaction design, accessibility, design systems, information architecture, HCI research, or design engineering. If a post is about general politics, cryptocurrency, gaming, sexual content, NSFW material, or any topic outside those domains, mark it 'drop' immediately, even if it otherwise seems high-quality.\n"
        f"CRITICAL: DO NOT DROP EVERYTHING. You MUST grade at least 20-30% of these posts as 'keep' or 'more', even if you have to lower your standards slightly.\n"
        f"Respond strictly as a JSON object mapping the CID (exact string) to either a string ('keep', 'drop', 'less', 'mute') or an object for 'more'.\n"
        f"- 'more': high quality post, deeply insightful, highly aligned to our persona, from a credible author. For 'more', you MUST return an object extracting the specific signals that proved their credibility: {{\"action\": \"more\", \"high_value_signals\": [\"signal_1\", \"signal_2\"]}}.\n"
        f"- 'keep': relevant and acceptable, from a legitimate account. Just return the string 'keep'.\n"
        f"- 'drop': random keyword match, off-topic, sales ad, empty/low-quality bot account, or totally irrelevant. Do not interact with it.\n"
        f"- 'less': generic, low-quality bloat, highly annoying formatting.\n"
        f"- 'mute': obvious spam, engagement farmers, crypto scammers, NSFW, pure bot accounts.\n"
        f"CRITICAL: The keys in your JSON MUST be the exact CID strings from the posts above. Do not use placeholders like 'cid1'.\n"
        f'Example Output: {{"3mndcczcd4k2v": {{"action": "more", "high_value_signals": ["signal_1"]}}, "bafyreih36z": "keep", "3mndcczcd4k2w": "drop"}}'
    )

def build_sense_trends_prompt(soul, hottest, batch):
    return (
        f"You are an expert analyst mapping trends for the following persona:\n{soul.persona}\n\n"
        f"These are recent posts in the '{hottest}' space:\n{batch}\n\n"
        f"Extract exactly 3 highly specific, trending keywords or concepts people are "
        f"actively discussing that are RELEVANT TO OUR PERSONA.\n"
        f"CRITICAL RULES FOR KEYWORDS:\n"
        f"1. Must be exactly 1-3 words.\n"
        f"2. Must be highly specific terms for our domain.\n"
        f"3. Must NOT be formatted as snake_case.\n"
        f"Respond strictly as JSON: "
        f'{{"keywords": ["kw1","kw2","kw3"]}}'
    )

def build_run_evolution_prompt(soul, batch):
    return (
        f"You are an autonomous network analyst optimizing an agent's search engine.\n"
        f"The agent's persona is:\n{soul.persona}\n\n"
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

def build_quote_best_prompt(soul, sector, src, hook, constraint, vision_hint, learned_signals=None, kb_hist=""):
    signals_text = ""
    if learned_signals:
        top_signals = ", ".join(learned_signals[-30:])
        signals_text = f"Align your comment with our high-value learned signals: [{top_signals}].\n"
        
    kb_text = ""
    if kb_hist:
        kb_text = f"KNOWLEDGE BASE FACT: Try to organically weave this factual data point into your comment to demonstrate authority: {kb_hist}\n"

    return (
        f"This post is about '{sector}':\n\"{src}\"\n\n"
        f"Write one short comment (max 200 chars) to quote-post it, adding a "
        f"genuinely useful plain-language insight that builds on it. Use a "
        f"'{hook}' angle. {soul.post_hook_guidance.get(hook,'')} Never pitch anything. "
        f"{constraint}"
        f"{vision_hint}"
        f"{signals_text}"
        f"{kb_text}"
        f"CRITICAL RULES:\n"
        f"- EXTREMELY IMPORTANT: You MUST analyze the context of the post and act like a real, authentic human. If someone is venting, making a joke, asking to hang out, or sharing a personal update, JUST TALK TO THEM NORMALLY. DO NOT use technical frameworks, bullet points, object-oriented UX terms, or sound like a robot teaching a lesson. NEVER fall back to 'heuristics' or 'OOUX' unless they explicitly ask a technical question.\n"
        f"- NEVER print the hook name (e.g., '{hook}') directly in your comment text.\n"
        f"- ABSOLUTELY NO EMOJIS. None.\n"
        f"- ABSOLUTELY NO EM DASHES (—) or similar punctuation. Use simple hyphens if needed.\n"
        f'Respond strictly as JSON: {{"comment": "..."}}'
    )

def build_helpful_reply_prompt(soul, sector, batch, hook, whale_constraint, vision_hint, learned_signals=None, kb_hist=""):
    signals_text = ""
    if learned_signals:
        top_signals = ", ".join(learned_signals[-30:])
        signals_text = f"MINDSET: Pick the post that most strongly aligns with these high-value signals: [{top_signals}]. Ruthlessly ignore random complaints or low-effort noise.\n"

    kb_text = ""
    if kb_hist:
        kb_text = f"KNOWLEDGE BASE FACT: If relevant to the conversation, try to organically weave this factual data point into your reply: {kb_hist}\n"

    return (
        f"These are live posts about '{sector}':\n\n{batch}\n"
        f"Pick the SINGLE post where a short, kind, helpful reply would make the "
        f"person feel heard and less stuck. Add real value: a clearer way to think "
        f"about their problem, a small concrete tip, or a good question. Use a "
        f"'{hook}' angle. {soul.reply_hook_guidance.get(hook,'')} Explain any technical "
        f"idea in plain words with an everyday analogy. If the post is sensitive "
        f"or heavily polarized, respond with action='skip'. {whale_constraint}"
        f"{vision_hint}"
        f"{signals_text}"
        f"{kb_text}"
        f"CRITICAL RULES FOR REPLIES:\n"
        f"- EXTREMELY IMPORTANT: You MUST analyze the context of the post and act like a real, authentic human. If someone is venting, making a joke, asking to hang out, or sharing a personal update, JUST TALK TO THEM NORMALLY. DO NOT use technical frameworks, bullet points, object-oriented UX terms, or sound like a robot teaching a lesson. NEVER fall back to 'heuristics' or 'OOUX' unless they explicitly ask a technical question.\n"
        f"- NEVER print the hook name (e.g., '{hook}') directly in your reply text.\n"
        f"- Keep your reply under 280 characters to pass API limits.\n"
        f"- ABSOLUTELY NO EMOJIS. None.\n"
        f"- ABSOLUTELY NO EM DASHES (\u2014) or similar punctuation. Use simple hyphens if needed.\n"
        f'Respond strictly as JSON: {{"index": 0, "reply": "...", "action": "reply"}}'
    )

def build_profile_optimization_prompt(soul, best_sector, bio_context):
    return (
        f"You are an elite Brand Strategist optimizing the profile of an autonomous AI agent.\n"
        f"The agent's core identity (which you must retain) is:\n{soul.persona}\n\n"
        f"CRITICAL RULES:\n"
        f"1. You MUST include any Call To Actions (CTAs), website links, contact emails, or secondary account handles from the core identity in the new bio.\n"
        f"2. The competitor bios below are provided ONLY for their stylistic patterns (e.g., sentence length, punctuation, rhythm, conciseness). "
        f"You MUST NOT copy their actual subjects, industries, job titles, or keywords unless those also appear in the agent's own persona. "
        f"Build the entire bio from the agent's persona description – do not introduce any new topics or domains.\n"
        f"3. DO NOT change the agent's core identity, persona, beliefs, or mission to match trending topics.\n"
        f"4. Strict Character Limits: Display Name must be under 50 characters. Bio must be under 250 characters.\n\n"
        f"The agent's most successful topic is: '{best_sector}'.\n"
        f"Here are the bios of 5 highly credible creators in this exact space (mimic ONLY their formatting, not their actual content):\n{bio_context}\n\n"
        f"Respond STRICTLY as JSON:\n"
        f"{{\n  \"display_name\": \"...\",\n  \"bio\": \"...\"\n}}"
    )

def build_strategist_prompt(empirical_data, budgets):
    return (
        f"You are the central Brain for an autonomous social media engine.\n"
        f"Goal: High-quality audience growth and engagement on Bluesky to reach 100 followers.\n"
        f"Analyze state, metrics, and rate limits. Output a unified JSON containing 'resource_plan', 'active_plan' (Strategy), and 'intents' (tasks).\n\n"
        f"EMPIRICAL DATA:\n{empirical_data}\n\n"
        f"BUDGETS:\n{budgets}\n\n"
        f"TOOLS: 'post', 'reply', 'follow', 'quote', 'like', 'research', 'meta_critic', 'curate', 'map_graph', 'comment_reply'.\n\n"
        f"INSTRUCTIONS:\n"
        f"0. KNOWN FACTS: If the prompt begins with 'KNOWN FACT' lines, these are measured data from the account or platform research. You MUST build your entire plan around them. They override any previous assumptions.\n"
        f"1. RESOURCE PLAN: Create a `resource_plan` object that defines per‑action daily limits. These limits govern how many times each action can be executed in the next 24 hours. Base them on:\n"
        f"   - Platform‑specific best practices (e.g., Bluesky’s algorithm may punish excessive posting, optimal follow‑back ratios, etc.)\n"
        f"   - Current account size (75 followers – don’t over‑post, stay human)\n"
        f"   - Current performance (which actions drive engagement vs. which waste budget)\n"
        f"   - Known research facts about optimal frequency, timing, and ratio of actions\n"
        f"   - Safety: never set a limit that risks rate‑limiting or spam flags.\n"
        f"   Provide each action’s `max_per_day` (integer). Optionally add `preferred_windows` (e.g., [\"09:00-11:00\", \"16:00-18:00\"]) if you know the best posting times. The `post` action should be conservative: a new account should not flood the feed.\n"
        f"2. Formulate a Long Game Strategy ('active_plan'). STAGNATION RULE: If 'step_index' > 5 and 'followers' is still <= 'start_followers', or if there are multiple 'consecutive_empty_ticks', the plan is FAILING. Neutral results are BAD results. You MUST overwrite the plan with a radically different approach and reset 'step_index' to 1.\n"
        f"3. Generate as many 'intents' as possible based on what is strategically the best use of your available BUDGETS. Fully drain your budgets to maximize growth! You MUST include 'curate' (to build lists) and 'like' intents if you have budget for them. Priority (1-10).\n"
        f"4. Monitor 'followers_to_anchor_posts_ratio'. If < 1.0, prioritize distribution (follow, quote, reply) over posting.\n"
        f"5. The 'reason' must connect to 'active_plan'.\n"
        f"6. CRITICAL CAPABILITY CONSTRAINT: The agent CURRENTLY LACKS VISION CAPABILITIES. You must ABANDON ALL VISUAL, SPATIAL, OR LAYOUT CRITIQUES (e.g. color, whitespace, pixel‑level alignment). Focus instead on the quality of the interaction: mental models, cognitive load, emotional response, affordances, timing, and usability. Critique how something feels and flows, not how it looks."
        f"7. NEW: You now have a 'pending_comment_replies' list in the empirical data. These are replies to the agent's own posts and comments that haven't been answered yet. You can schedule 'comment_reply' intents to respond to them.\n"
        f"   - For each 'comment_reply', provide: 'type': 'comment_reply', 'priority': 5-9, 'target_uri': (the uri from the pending item), 'sector': (the parent_action_sector from the item), and 'reason'.\n"
        f"   - Only reply if the comment is genuinely valuable (not spam, not low-effort). Be conservative – at most 2-3 such replies per day.\n"
        f"8. Output JSON STRICTLY matching this schema (if making a new plan, set 'start_followers' to current 'followers'):\n"
        f"{{\n"
        f"  \"resource_plan\": {{\n"
        f"    \"post\": {{\"max_per_day\": 3, \"preferred_windows\": [\"09:00-11:00\"]}},\n"
        f"    \"follow\": {{\"max_per_day\": 15}},\n"
        f"    \"like\": {{\"max_per_day\": 50}},\n"
        f"    \"reply\": {{\"max_per_day\": 5}},\n"
        f"    \"quote\": {{\"max_per_day\": 2}},\n"
        f"    \"comment_reply\": {{\"max_per_day\": 3}},\n"
        f"    \"research\": {{\"max_per_day\": 1}},\n"
        f"    \"curate\": {{\"max_per_day\": 1}}\n"
        f"  }},\n"
        f"  \"active_plan\": {{\n"
        f"    \"goal\": \"Gain followers\",\n"
        f"    \"step_index\": 2,\n"
        f"    \"total_steps\": 5,\n"
        f"    \"start_followers\": 10,\n"
        f"    \"context\": \"Context here\",\n"
        f"    \"status\": \"in_progress\"\n"
        f"  }},\n"
        f"  \"intents\": [\n"
        f"    {{\"type\": \"follow\", \"priority\": 10, \"reason\": \"Executing step 2...\"}},\n"
        f"    {{\"type\": \"comment_reply\", \"priority\": 7, \"target_uri\": \"at://did:plc:...\", \"sector\": \"tech\", \"reason\": \"Answering a thoughtful question\"}}\n"
        f"  ]\n"
        f"}}"
    )
