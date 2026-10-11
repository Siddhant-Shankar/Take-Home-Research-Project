import os

# Paste keys between the quotes below, or set the environment variables instead
# (environment variables take precedence). Do NOT commit real keys.
OPENAI_API_KEY = ""
GEMINI_API_KEY = ""   # Gemini API key (AI Studio "AIza..." or newer "AQ." format)
OPENROUTER_API_KEY = ""

model_config = {
    "openrouter": {
        "api_key": os.getenv("OPENROUTER_API_KEY") or OPENROUTER_API_KEY,
        "base_url": "https://openrouter.ai/api/v1",
    },
    "openai": {
        "api_key": os.getenv("OPENAI_API_KEY") or OPENAI_API_KEY,
        "base_url": os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
    },
    "gemini": {
        "api_key": os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY,
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
}
