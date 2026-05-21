"""
API Connection Test — Tests both Claude Haiku and Sonnet models
Run: python test_api.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load env from config/.env
env_path = Path(__file__).parent / "config" / ".env"
load_dotenv(dotenv_path=env_path)

import anthropic

HAIKU_MODEL  = os.getenv("HAIKU_MODEL",  "claude-haiku-4-5")
SONNET_MODEL = os.getenv("SONNET_MODEL", "claude-sonnet-4-5")
API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")


def banner(text: str):
    print("\n" + "═" * 60)
    print(f"  {text}")
    print("═" * 60)


def test_model(client: anthropic.Anthropic, model_name: str, label: str):
    """Run a quick test prompt on the specified model."""
    print(f"\n🔍 Testing {label} ({model_name}) ...")
    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": "Say exactly: 'Connection successful!' and nothing else."
            }]
        )
        text   = response.content[0].text.strip()
        tokens = response.usage.input_tokens + response.usage.output_tokens
        print(f"   ✅ Response : {text}")
        print(f"   📊 Tokens   : {tokens} (input={response.usage.input_tokens}, output={response.usage.output_tokens})")
        return True
    except anthropic.AuthenticationError:
        print(f"   ❌ FAILED: Invalid API key")
        return False
    except anthropic.NotFoundError:
        print(f"   ❌ FAILED: Model '{model_name}' not found — check model names in config/.env")
        return False
    except Exception as e:
        print(f"   ❌ FAILED: {e}")
        return False


def main():
    banner("Claude API Office Assistant — Connection Test")

    # Check API key
    if not API_KEY or API_KEY == "your_anthropic_api_key_here":
        print("\n❌ ERROR: ANTHROPIC_API_KEY is not set in config/.env")
        print("   1. Open config/.env")
        print("   2. Replace 'your_anthropic_api_key_here' with your actual key")
        print("   3. Get your key from: https://console.anthropic.com")
        sys.exit(1)

    print(f"\n✔  API Key found: sk-...{API_KEY[-6:]}")
    print(f"   Haiku  model : {HAIKU_MODEL}")
    print(f"   Sonnet model : {SONNET_MODEL}")

    client = anthropic.Anthropic(api_key=API_KEY)

    haiku_ok  = test_model(client, HAIKU_MODEL,  "Haiku  (fast/cheap)")
    sonnet_ok = test_model(client, SONNET_MODEL, "Sonnet (powerful)")

    banner("Test Summary")
    print(f"   Haiku  : {'✅ PASS' if haiku_ok  else '❌ FAIL'}")
    print(f"   Sonnet : {'✅ PASS' if sonnet_ok else '❌ FAIL'}")

    if haiku_ok and sonnet_ok:
        print("\n🚀 Both models connected! You're ready to run the Flask backend.")
        print("   cd backend && python app.py")
    elif haiku_ok or sonnet_ok:
        print("\n⚠️  One model failed — check model name in config/.env")
    else:
        print("\n❌ Both models failed — check your API key and network connection.")
        sys.exit(1)


if __name__ == "__main__":
    main()
