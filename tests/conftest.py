import os

os.environ.setdefault("BOT_SKIP_STARTUP", "1")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER", "+390000000000")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
