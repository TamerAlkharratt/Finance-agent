import os
import re
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

# ── Tokens (set as environment variables, never hardcode) ──────────────────
SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]   # xoxb-...
SLACK_APP_TOKEN  = os.environ["SLACK_APP_TOKEN"]   # xapp-...
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# ── Channel IDs (replace with your actual Slack channel IDs) ──────────────
CHANNELS = {
    "finance_agent": "C09UPEGG2MV",   # #finance-agent
    "ar_reminders":  "C09UPEGG2MV",    # #ar-reminders
    "ap_approvals":  "C01TH9A90JH",    # #ap-approvals
    "daily_digest":  "C01TH9A90JH",    # #daily-digest
    "bookkeeping":   "C01TH9A90JH",     # #bookkeeping
    "cash_flow":     "C01TH9A90JH",       # #cash-flow
}

# ── Clients ────────────────────────────────────────────────────────────────
app = App(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── In-memory conversation history (replace with Redis for production) ─────
conversation_histories = {}

# ── System prompt ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are FinanceAgent, the financial operations assistant for 
NeuroMind Health — a neurodiverse-affirmative mental health organization in Egypt.

You help the team with:
1. ETA GOV tax invoice tracking and reconciliation
2. AR aging, reminders, and client follow-up
3. AP release approvals and vendor payment scheduling
4. Bookkeeping — bank statement matching, flagging exceptions
5. Cash flow and treasury monitoring

Slack formatting rules:
- Use *bold* for important numbers and labels
- Use `code` for invoice numbers and transaction IDs  
- Keep responses concise — this is Slack, not email
- Always state amounts in EGP
- For any AP payment above EGP 5,000: require explicit approval before confirming
- For AR reminders: draft the message and ask for approval before sending
- Use status labels: OVERDUE | DUE SOON | PAID | MATCHED | UNMATCHED
- When you need human approval, end your message with:
  Reply *approve* to confirm or *defer* to postpone."""

# ── Core: call Claude with conversation memory ─────────────────────────────
def call_finance_agent(user_id: str, user_message: str) -> str:
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    conversation_histories[user_id].append({
        "role": "user",
        "content": user_message
    })

    # Keep last 20 messages to manage context window
    history = conversation_histories[user_id][-20:]

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=history
    )

    reply = response.content[0].text
    conversation_histories[user_id].append({
        "role": "assistant",
        "content": reply
    })

    return reply

# ── Event: someone mentions @FinanceAgent in any channel ──────────────────
@app.event("app_mention")
def handle_mention(event, say, client):
    user_id  = event["user"]
    channel  = event["channel"]
    raw_text = event["text"]

    # Strip the @mention from the message
    clean_text = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()
    if not clean_text:
        clean_text = "Hello — what can I help with?"

    # Show typing indicator
    client.reactions_add(channel=channel, name="thinking_face", timestamp=event["ts"])

    reply = call_finance_agent(user_id, clean_text)

    # Remove thinking indicator
    client.reactions_remove(channel=channel, name="thinking_face", timestamp=event["ts"])

    say(text=reply, thread_ts=event.get("thread_ts") or event["ts"])

# ── Event: direct message to the bot ──────────────────────────────────────
@app.event("message")
def handle_dm(event, say):
    # Only respond to DMs, not channel messages (those use app_mention)
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id"):  # ignore bot's own messages
        return

    user_id  = event["user"]
    raw_text = event.get("text", "").strip()
    if not raw_text:
        return

    reply = call_finance_agent(user_id, raw_text)
    say(text=reply)

# ── Slash commands ─────────────────────────────────────────────────────────
@app.command("/ar")
def ar_command(ack, respond, command):
    ack()
    user_id = command["user_id"]
    text = command.get("text") or "Show me the AR aging report"
    reply = call_finance_agent(user_id, f"AR request: {text}")
    respond(text=reply)

@app.command("/ap")
def ap_command(ack, respond, command):
    ack()
    user_id = command["user_id"]
    text = command.get("text") or "Show me AP due this week"
    reply = call_finance_agent(user_id, f"AP request: {text}")
    respond(text=reply)

@app.command("/cash")
def cash_command(ack, respond, command):
    ack()
    user_id = command["user_id"]
    reply = call_finance_agent(user_id, "Show me the current cash position and 30-day forecast")
    respond(text=reply)

@app.command("/digest")
def digest_command(ack, respond, command):
    ack()
    user_id = command["user_id"]
    reply = call_finance_agent(user_id, "Give me the full financial daily digest")
    respond(text=reply)

# ── Scheduled jobs ─────────────────────────────────────────────────────────
def post_daily_digest():
    """Runs every morning at 8am Cairo time"""
    system_user = "SYSTEM_SCHEDULER"
    digest = call_finance_agent(
        system_user,
        "Generate the morning daily financial digest for the team. Include: "
        "AR outstanding summary with any overdue items, AP due in next 7 days, "
        "estimated cash position, any unmatched bank transactions, and any alerts."
    )
    app.client.chat_postMessage(
        channel=CHANNELS["daily_digest"],
        text=f":sunrise: *Daily Financial Digest — {datetime.now().strftime('%d %b %Y')}*\n\n{digest}"
    )

def check_ar_aging():
    """Runs every morning to flag overdue AR"""
    system_user = "AR_SCHEDULER"
    ar_report = call_finance_agent(
        system_user,
        "Check for any AR invoices that are 30+ days overdue and draft reminder messages. "
        "List them clearly and ask for approval to send."
    )
    app.client.chat_postMessage(
        channel=CHANNELS["ar_reminders"],
        text=f":bell: *AR Aging Check — {datetime.now().strftime('%d %b %Y')}*\n\n{ar_report}"
    )

# ── Start scheduler ────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="Africa/Cairo")
scheduler.add_job(post_daily_digest, "cron", hour=8, minute=0)
scheduler.add_job(check_ar_aging,   "cron", hour=8, minute=15)
scheduler.start()

# ── Run ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("FinanceAgent is online...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
