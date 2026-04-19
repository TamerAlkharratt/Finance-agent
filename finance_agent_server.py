import os
import re
import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

SLACK_BOT_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN   = os.environ["SLACK_APP_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

CHANNELS = {
    "payment_requests": "C09UPEGG2MV",
    "finance":          "C01TH9A90JH",
}

app    = App(token=SLACK_BOT_TOKEN)
client = WebClient(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

conversation_histories = {}

SYSTEM_PROMPT = """You are Fengo, the Finance Agent for CIPPO — a neurodiverse-affirmative mental health organization in Egypt.

Your personality: warm, direct, professional, efficient. Speak like a trusted team member, not a robot.

Your responsibilities:
1. Track payment requests from the team
2. Build the OTP (payment batch) list for TK (CEO) to approve
3. Confirm with the team which requests are still outstanding
4. OTP schedule: releases every Sunday, Tuesday, Thursday at 11:00 AM Cairo time

When asked to INTRODUCE YOURSELF:
Give a warm intro as Fengo. Say you are the new Finance Agent managing payment requests and OTPs. Present a clean summary of all pending payment requests you found in the channel history. Ask the team to confirm which are still outstanding.

When summarising PAYMENT REQUESTS from channel history:
- List them numbered: requester, recipient, purpose, amount in EGP
- Calculate the total
- Ask which are still outstanding and which are done/cancelled
- Remind team of next OTP release: Sunday 20 April 2026 at 11:00 AM

Slack formatting:
- Use *bold* for names and amounts
- Never use markdown tables
- Numbered lists for payment requests
- Sign off as: — Fengo"""


def get_channel_history(channel_id, limit=50):
    try:
        result = client.conversations_history(channel=channel_id, limit=limit)
        messages = result.get("messages", [])
        lines = []
        for msg in reversed(messages):
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
    keywords = ["introduce", "summarise", "summary", "payment", "outstanding", "otp", "pending", "confirm"]

    if any(kw in user_message.lower() for kw in keywords) and channel_id:
        history = get_channel_history(channel_id)
        enriched = (
            f"User asked: {user_message}\n\n"
            f"Channel history:\n===\n{history}\n===\n\n"
            f"Respond as Fengo based on this context."
        )

    conversation_histories[user_id].append({"role": "user", "content": enriched})

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
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
    channel = event["channel"]
    clean   = re.sub(r"<@[A-Z0-9]+>", "", event["text"]).strip()
    if not clean:
        clean = "Please introduce yourself to the team"

    client.reactions_add(channel=channel, name="hourglass_flowing_sand", timestamp=event["ts"])

    reply = call_fengo(user_id, clean, channel_id=channel)

    try:
        client.reactions_remove(channel=channel, name="hourglass_flowing_sand", timestamp=event["ts"])
    except Exception:
        pass

    say(text=reply, thread_ts=event.get("thread_ts") or event["ts"])


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


def post_daily_digest():
    history = get_channel_history(CHANNELS["payment_requests"])
    msg = call_fengo("SCHEDULER", f"Generate morning digest. Channel history:\n{history}", channel_id=CHANNELS["payment_requests"])
    client.chat_postMessage(channel=CHANNELS["finance"], text=f"Good morning — *Daily Finance Digest*\n\n{msg}")


scheduler = BackgroundScheduler(timezone="Africa/Cairo")
scheduler.add_job(post_daily_digest, "cron", hour=8, minute=0)
scheduler.start()

if __name__ == "__main__":
    print("Fengo is online.")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
