import os
import re
import anthropic
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

SLACK_BOT_TOKEN   = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN   = os.environ.get("SLACK_APP_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN or not ANTHROPIC_API_KEY:
    raise RuntimeError("Missing required environment variables: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ANTHROPIC_API_KEY")

CHANNELS = {
    "payment_requests": "C09UPEGG2MV",
    "finance":          "C01TH9A90JH",
}

app    = App(token=SLACK_BOT_TOKEN)
client = WebClient(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

conversation_histories = {}

SYSTEM_PROMPT = """You are Fengo, the Finance Agent for CIPPO.
Personality: warm, direct, professional, efficient. Speak like a trusted team member.

When asked to introduce yourself:
- Give a warm intro as Fengo, the new Finance Agent for CIPPO
- Say you manage payment requests and OTPs
- Present a numbered summary of all pending payment requests from the channel history
- Ask the team to confirm which are still outstanding

When summarising payment requests:
- Number each one: requester, recipient, purpose, amount in EGP
- Calculate the total
- Ask which are still outstanding and which are done or cancelled
- Remind them: next OTP release is Sunday 20 April 2026 at 11:00 AM Cairo time

Slack formatting:
- Use *bold* for names and amounts
- Never use markdown tables
- Numbered lists for payment requests
- Sign off as: -- Fengo"""


def get_channel_history(channel_id, limit=50):
    try:
        result = client.conversations_history(channel=channel_id, limit=limit)
        lines = []
        for msg in reversed(result.get("messages", [])):
            if msg.get("subtype"):
                continue
            text = msg.get("text", "").strip()
            if text and len(text) > 10:
                lines.append(text)
        return "\n---\n".join(lines)
    except Exception as e:
        return f"Could not read channel history: {e}"


def call_fengo(user_id, user_message, channel_id=None):
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    enriched = user_message
    keywords = ["introduce", "summarise", "summary", "payment", "outstanding",
                "otp", "pending", "confirm", "hello", "requests"]
    if any(kw in user_message.lower() for kw in keywords) and channel_id:
        history = get_channel_history(channel_id)
        enriched = (
            f"User asked: {user_message}\n\n"
            f"Channel history:\n===\n{history}\n===\n\n"
            f"Respond as Fengo."
        )

    conversation_histories[user_id].append({"role": "user", "content": enriched})

    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=conversation_histories[user_id][-20:]
    )
    reply = response.content[0].text
    conversation_histories[user_id].append({"role": "assistant", "content": reply})
    return reply


@app.event("app_mention")
def handle_mention(event, say, client):
    user_id = event["user"]
    channel  = event["channel"]
    clean    = re.sub(r"<@[A-Z0-9]+>", "", event["text"]).strip()
    if not clean:
        clean = "Please introduce yourself"

    try:
        client.reactions_add(channel=channel, name="hourglass_flowing_sand", timestamp=event["ts"])
    except Exception:
        pass

    reply = call_fengo(user_id, clean, channel_id=channel)

    try:
        client.reactions_remove(channel=channel, name="hourglass_flowing_sand", timestamp=event["ts"])
    except Exception:
        pass

    say(text=reply)


@app.event("message")
def handle_dm(event, say):
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id"):
        return
    user_id = event["user"]
    text    = event.get("text", "").strip()
    if not text:
        return
    say(text=call_fengo(user_id, text))


scheduler = BackgroundScheduler(timezone="Africa/Cairo")
scheduler.start()

if __name__ == "__main__":
    print("Fengo is online.")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
