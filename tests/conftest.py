import os

# Set required environment variables before any kernos.* module is imported.
# load_dotenv() in app.py does not override existing env vars, so these take priority.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("OWNER_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+12345678901")

# Override KERNOS_INSTANCE_ID to empty during tests so adapters use fallback derivation.
# load_dotenv() won't override existing env vars, so setting it empty blocks .env leakage.
os.environ["KERNOS_INSTANCE_ID"] = ""

# Force Anthropic provider in tests so .env's KERNOS_LLM_PROVIDER doesn't bleed in.
os.environ["KERNOS_LLM_PROVIDER"] = "anthropic"
