"""Build the Strands agent.

Reads the LLM settings from .env. Works with any OpenAI-compatible endpoint; the
defaults below match Groq:
    GROQ_API_KEY=...                                   (or OPENAI_API_KEY)
    OPENAI_API_BASE=https://api.groq.com/openai/v1
    MODEL_ID=openai/gpt-oss-120b
"""
import logging
import os

import config  # noqa: F401  (importing it loads the .env file)
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.openai import OpenAIModel

from agent.prompts import SYSTEM_PROMPT
from agent.tools import ALL_TOOLS


# Groq's gpt-oss models return "reasoning" text; Strands warns (harmlessly) that it cannot replay it
logging.getLogger("strands.models.openai").setLevel(logging.ERROR)


def build_model() -> OpenAIModel:
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Set GROQ_API_KEY (or OPENAI_API_KEY) in .env")
    return OpenAIModel(
        client_args={
            "api_key": api_key,
            "base_url": os.getenv("OPENAI_API_BASE", "https://api.groq.com/openai/v1"),
        },
        model_id=os.getenv("MODEL_ID", "openai/gpt-oss-120b"),
        params={"max_tokens": 1500, "temperature": 0.2},
    )


def build_agent() -> Agent:
    return Agent(
        model=build_model(),
        system_prompt=SYSTEM_PROMPT,
        tools=ALL_TOOLS,
        callback_handler=None,  # do not print to the terminal; the caller shows the answer
        conversation_manager=SlidingWindowConversationManager(window_size=8),  # keep token use bounded
    )
