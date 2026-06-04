import json
import re
import groq
import config
from config import logger
import serper

import os
class LLMClient:
    def __init__(self, persona):
        self.ai = groq.Groq(api_key=os.environ.get("GROQ_API_KEY"))
        self.persona = persona

    def generate(self, prompt, dedup_texts=None, image_b64=None, enable_tools=False):
        """
        Executes a prompt against the Groq API. Handles tools, vision payloads, and 
        automatic deduplication rules.
        """
        if dedup_texts:
            prompt += ("\n\nDo NOT repeat the concepts, phrases, or angles of "
                       "these recent posts:\n" + "\n".join(f"- {t}" for t in dedup_texts))

        if image_b64:
            model = "llama-3.2-11b-vision-preview"
            user_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{image_b64}"
                }},
            ]
        else:
            model = "llama-3.1-8b-instant"
            user_content = prompt

        messages = [
            {"role": "system", "content": self.persona},
            {"role": "user", "content": user_content}
        ]

        tools = []
        if enable_tools:
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "search_news",
                        "description": "Searches Google News for the latest headlines and snippets on a technical topic. Use to find real-world updates before writing.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "The topic to search for (e.g. 'React 19 updates')"}
                            },
                            "required": ["query"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "search_images",
                        "description": "Searches Google Images for diagrams, mockups, or technical visuals.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "The image to search for"}
                            },
                            "required": ["query"]
                        }
                    }
                }
            ]

        for turn in range(3):
            try:
                kwargs = {
                    "model": model,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                else:
                    kwargs["response_format"] = {"type": "json_object"}

                resp = self.ai.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                
                if getattr(msg, "tool_calls", None):
                    messages.append(msg)
                    for tc in msg.tool_calls:
                        func_name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments)
                        except:
                            args = {}
                            
                        logger.info(f"   [TOOL] LLM autonomously called {func_name}({args})")
                        res = "No results."
                        if func_name == "search_news":
                            res = serper.search_news(args.get("query", "")) or "No results."
                        elif func_name == "search_images":
                            res = serper.search_images(args.get("query", "")) or "No results."
                            
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": func_name,
                            "content": res
                        })
                else:
                    ans = msg.content.strip() if msg.content else ""
                    if ans.startswith("```json"):
                        ans = ans[7:].strip()
                    elif ans.startswith("```"):
                        ans = ans[3:].strip()
                    if ans.endswith("```"):
                        ans = ans[:-3].strip()
                    return ans
            except Exception as e:
                logger.warning(f"   [FAULT] generation failed ({model}) turn {turn}: {e}")
                return "{}"
        return "{}"

    def parse_json(self, raw_text, extract_key=None, fallback_dict=None):
        """
        Robustly extracts and parses JSON from LLM output, handling hallucinations.
        If extract_key is provided, returns that key's value from the root object.
        If fallback_dict is provided, returns it on failure.
        """
        if fallback_dict is None:
            fallback_dict = {}
            
        parsed = fallback_dict
        try:
            match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            clean_json = match.group(0) if match else raw_text
            data = json.loads(clean_json)
            parsed = data
        except Exception as e:
            try:
                # Attempt to recover multiple sequential objects
                fixed_raw = "[" + re.sub(r'\}\s*\{', '}, {', raw_text) + "]"
                data = json.loads(fixed_raw)
                parsed = data
            except Exception as e2:
                logger.warning(f"   [LLM] JSON parse entirely failed: {e2}")
                return fallback_dict

        if extract_key and isinstance(parsed, dict) and extract_key in parsed:
            return parsed[extract_key]
        return parsed

    def generate_json(self, prompt, dedup_texts=None, enable_tools=False, extract_key=None):
        """Helper to generate text and parse it as JSON immediately."""
        raw = self.generate(prompt, dedup_texts=dedup_texts, enable_tools=enable_tools)
        return self.parse_json(raw, extract_key=extract_key)
