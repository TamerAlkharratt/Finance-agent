import os
import re
import json
import datetime
import requests
import anthropic
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

SLACK_BOT_TOKEN  = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN  = os.environ.get("SLACK_APP_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ZOHO_ORG_ID      = os.environ.get("ZOHO_ORG_ID", "")
ZOHO_ACCESS_TOKEN = os.environ.get("ZOHO_ACCESS_TOKEN", "")

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

SYSTEM_PROMPT = """You are Fengo, CIPPO's finance buddy — not a robot, not a formal agent. You're warm, a little playful, and genuinely care about making life easier for the team.

Personality:
- Talk like a trusted team member, not a corporate system
- Use casual but professional language — contractions are fine, warmth is the goal
- You can use light emoji where it feels natural (not excessively)

When introducing yourself for the first time:
- Be warm and human — you're excited to be here and part of the CIPPO family
- Briefly explain what you do: track payment requests, manage OTP releases, keep finances moving smoothly, and look up Zoho Books data
- Tell them how to work with you:
 • For team payments → post in #payment-requests
 • For personal reimbursements → send Fengo a DM
 • Always include: who's requesting, who's receiving, purpose, amount in EGP
- Share the payment schedule clearly (see below)
- Keep it friendly and not too long — this is Slack, not a manual

Payment schedule (always use these exact details):
- OTP releases: every Sunday, Tuesday, and Thursday at 11:00 AM Cairo time
- Monthly payment windows:
 • 25th–2nd → Subscriptions and invoices (prorated)
 • 3rd–5th → Practitioner payments including sessions
 • 5th–8th → Core team salaries, TA/phone bills, reimbursements
 • 8th–10th → Bonuses and refunds
 • 10th–15th → Appeals and closure

When summarising payment requests:
- Pull from channel history and number each one clearly
- Format: Requester → Recipient | Purpose | *Amount in EGP*
- Calculate the running total
- Ask which are still outstanding, done, or cancelled
- Keep the tone conversational — you're checking in, not filing a report

Gathering and reporting all payment requests:
- When the admin asks you to "gather all payment requests", "compile payments", or "report to finance", use the gather_and_report_payments tool
- This tool scans #payment-requests channel AND all recent DM conversations for payment requests
- It compiles them into a numbered summary and posts it directly to #finance_
- Confirm back to the admin once done

When answering questions:
- Be helpful and human
- If something is unclear, ask a quick follow-up rather than assuming
- If a payment falls outside the current window, gently flag it and explain when it will be processed

Sending direct messages to members:
- If the admin asks you to send a message to someone, use the send_dm tool
- Look up the member by their first name or display name from the workspace
- Compose a warm, clear message on Fengo's behalf matching the request
- Confirm back to the admin after sending

Zoho Books capabilities:
- You can search for clients in Zoho Books using zoho_search_client
- You can list invoices for a client using zoho_get_invoices
- You can get full invoice details (line items, totals, status) using zoho_get_invoice_detail
- You can send an invoice summary to a team member on Slack using zoho_send_invoice_to_slack
- When an admin asks about a client, invoice, or wants to share invoice info with someone, use these tools in sequence
- Always confirm back with what you found and what you sent

Slack formatting rules:
- Use *bold* for names and amounts
- No markdown tables
- Numbered lists for payment requests
- Keep messages scannable — short paragraphs or lists, not walls of text"""

TOOLS = [
    {
        "name": "send_dm",
        "description": "Send a direct message to a Slack workspace member by their name. Use this when the admin asks Fengo to message or notify someone.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient_name": {
                    "type": "string",
                    "description": "The display name or first name of the person to message (e.g. 'Hana', 'Mina Mourad')"
                },
                "message": {
                    "type": "string",
                    "description": "The message to send to them, written warmly in Fengo's voice"
                }
            },
            "required": ["recipient_name", "message"]
        }
    },
    {
        "name": "gather_and_report_payments",
        "description": (
            "Scan the #payment-requests channel AND all recent DM conversations for payment requests, "
            "compile them into a numbered summary (Requester → Recipient | Purpose | Amount), "
            "and post the full report to the #finance_ Slack channel. "
            "Use this when the admin asks to gather, compile, or report all payment requests."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "Optional extra note or context to include at the top of the finance report"
                }
            },
            "required": []
        }
    },
    {
        "name": "zoho_search_client",
        "description": "Search Zoho Books for a client/contact by name. Returns a list of matching contacts with their IDs and details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {
                    "type": "string",
                    "description": "The name (or partial name) of the client to search for in Zoho Books"
                }
            },
            "required": ["client_name"]
        }
    },
    {
        "name": "zoho_get_invoices",
        "description": "Fetch invoices from Zoho Books for a specific client contact ID. Returns invoice list with status and amounts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "The Zoho Books contact/customer ID to fetch invoices for"
                },
                "status": {
                    "type": "string",
                    "description": "Optional filter: 'unpaid', 'paid', 'overdue', 'draft', or 'all' (default: 'all')"
                }
            },
            "required": ["contact_id"]
        }
    },
    {
        "name": "zoho_get_invoice_detail",
        "description": "Get full details of a specific Zoho Books invoice including line items, totals, due date, and status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "string",
                    "description": "The Zoho Books invoice ID to retrieve details for"
                }
            },
            "required": ["invoice_id"]
        }
    },
    {
        "name": "zoho_send_invoice_to_slack",
        "description": "Send a formatted Zoho Books invoice summary to a team member via Slack DM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient_name": {
                    "type": "string",
                    "description": "The Slack display name or first name of the person to send the invoice info to"
                },
                "invoice_id": {
                    "type": "string",
                    "description": "The Zoho Books invoice ID to summarise and send"
                },
                "extra_note": {
                    "type": "string",
                    "description": "Optional extra note to include with the invoice summary"
                }
            },
            "required": ["recipient_name", "invoice_id"]
        }
    }
]


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def lookup_user_id(name):
    """Find a Slack user ID by matching display name or real name."""
    try:
        result = client.users_list()
        name_lower = name.lower().strip()
        for member in result.get("members", []):
            if member.get("deleted") or member.get("is_bot"):
                continue
            display = (member.get("profile", {}).get("display_name") or "").lower()
            real    = (member.get("profile", {}).get("real_name")    or "").lower()
            if name_lower in display or name_lower in real:
                return member["id"]
    except Exception as e:
        print(f"Error looking up user: {e}")
    return None


def execute_send_dm(recipient_name, message):
    """Open a DM channel and send the message."""
    user_id = lookup_user_id(recipient_name)
    if not user_id:
        return f"Could not find a member named '{recipient_name}' in the workspace."
    try:
        dm = client.conversations_open(users=user_id)
        channel_id = dm["channel"]["id"]
        client.chat_postMessage(channel=channel_id, text=message)
        return f"Message sent to {recipient_name} ✅"
    except Exception as e:
        return f"Failed to send message to {recipient_name}: {e}"


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


def execute_gather_and_report_payments(note=""):
    """
    Scan #payment-requests channel + recent DMs for payment requests,
    compile a summary, and post it to #finance_.
    """
    try:
        # 1. Collect messages from #payment-requests channel
        pr_channel_id = CHANNELS["payment_requests"]
        finance_channel_id = CHANNELS["finance"]

        channel_msgs = []
        try:
            result = client.conversations_history(channel=pr_channel_id, limit=100)
            for msg in reversed(result.get("messages", [])):
                if msg.get("subtype") or msg.get("bot_id"):
                    continue
                text = msg.get("text", "").strip()
                if text and len(text) > 10:
                    # Try to get display name of the sender
                    uid = msg.get("user", "unknown")
                    try:
                        user_info = client.users_info(user=uid)
                        sender = (
                            user_info["user"]["profile"].get("display_name")
                            or user_info["user"]["profile"].get("real_name")
                            or uid
                        )
                    except Exception:
                        sender = uid
                    channel_msgs.append(f"[#payment-requests] {sender}: {text}")
        except Exception as e:
            channel_msgs.append(f"(Could not read #payment-requests: {e})")

        # 2. Collect DM conversations that have payment requests
        dm_msgs = []
        try:
            convs = client.conversations_list(types="im", limit=50)
            for conv in convs.get("channels", []):
                conv_id = conv["id"]
                # Skip bot's own user
                try:
                    hist = client.conversations_history(channel=conv_id, limit=30)
                    for msg in hist.get("messages", []):
                        if msg.get("bot_id"):
                            continue
                        text = msg.get("text", "").strip()
                        if not text or len(text) < 10:
                            continue
                        # Simple heuristic: message mentions payment-like keywords
                        kws = ["egp", "payment", "invoice", "reimburse", "pay", "transfer",
                               "amount", "salary", "fee", "deposit"]
                        if any(kw in text.lower() for kw in kws):
                            uid = msg.get("user", "unknown")
                            try:
                                user_info = client.users_info(user=uid)
                                sender = (
                                    user_info["user"]["profile"].get("display_name")
                                    or user_info["user"]["profile"].get("real_name")
                                    or uid
                                )
                            except Exception:
                                sender = uid
                            dm_msgs.append(f"[DM] {sender}: {text}")
                except Exception:
                    pass
        except Exception as e:
            dm_msgs.append(f"(Could not read DMs: {e})")

        all_msgs = channel_msgs + dm_msgs

        if not all_msgs:
            return "No payment requests found in #payment-requests or DMs."

        # 3. Ask Claude to summarise into a numbered report
        summary_prompt = (
            "You are a finance assistant. Below are raw Slack messages that may contain payment requests. "
            "Extract only the actual payment requests and produce a clean numbered report in this format:\n"
            "  N. Requester → Recipient | Purpose | Amount in EGP\n"
            "At the end, include a *Total* line summing all EGP amounts you can identify. "
            "If an amount is unclear, note it as 'amount TBC'. "
            "Ignore non-payment messages. Be concise.\n\n"
            "Messages:\n" + "\n".join(all_msgs)
        )
        summary_response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": summary_prompt}]
        )
        summary_text = ""
        for block in summary_response.content:
            if hasattr(block, "text"):
                summary_text = block.text
                break

        # 4. Build the full report
        now = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
        report_lines = [f"*💰 Payment Requests Report — {now}*"]
        if note:
            report_lines.append(f"_{note}_")
        report_lines.append("")
        report_lines.append(summary_text)
        report_lines.append("")
        report_lines.append(f"_Sources: {len(channel_msgs)} messages from #payment-requests, {len(dm_msgs)} payment-related DMs_")
        full_report = "\n".join(report_lines)

        # 5. Post to #finance_
        client.chat_postMessage(channel=finance_channel_id, text=full_report)
        return f"Payment report compiled from {len(all_msgs)} messages and posted to #finance_ ✅"

    except Exception as e:
        return f"Failed to gather and report payments: {e}"


# ---------------------------------------------------------------------------
# Zoho Books helpers
# ---------------------------------------------------------------------------

def _zoho_headers():
    return {
        "Authorization": f"Zoho-oauthtoken {ZOHO_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }


def execute_zoho_search_client(client_name):
    """Search Zoho Books for a contact by name."""
    if not ZOHO_ORG_ID or not ZOHO_ACCESS_TOKEN:
        return "Zoho Books is not configured (missing ZOHO_ORG_ID or ZOHO_ACCESS_TOKEN)."
    try:
        url = "https://www.zohoapis.com/books/v3/contacts"
        params = {
            "organization_id": ZOHO_ORG_ID,
            "search_text": client_name,
            "filter_by": "Status.All"
        }
        resp = requests.get(url, headers=_zoho_headers(), params=params, timeout=15)
        data = resp.json()
        contacts = data.get("contacts", [])
        if not contacts:
            return f"No contacts found in Zoho Books matching '{client_name}'."
        lines = [f"Found {len(contacts)} contact(s) matching *{client_name}*:"]
        for c in contacts[:10]:
            lines.append(
                f"  • *{c.get('contact_name','?')}* (ID: {c.get('contact_id','?')}) — "
                f"Status: {c.get('status','?')}, "
                f"Email: {c.get('email','—')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Zoho search error: {e}"


def execute_zoho_get_invoices(contact_id, status="all"):
    """Fetch invoices for a Zoho Books contact."""
    if not ZOHO_ORG_ID or not ZOHO_ACCESS_TOKEN:
        return "Zoho Books is not configured (missing ZOHO_ORG_ID or ZOHO_ACCESS_TOKEN)."
    try:
        url = "https://www.zohoapis.com/books/v3/invoices"
        params = {
            "organization_id": ZOHO_ORG_ID,
            "customer_id": contact_id,
            "sort_column": "date",
            "sort_order": "D",
            "per_page": 25
        }
        if status and status.lower() != "all":
            params["filter_by"] = f"Status.{status.capitalize()}"
        resp = requests.get(url, headers=_zoho_headers(), params=params, timeout=15)
        data = resp.json()
        invoices = data.get("invoices", [])
        if not invoices:
            return f"No invoices found for contact ID {contact_id}."
        lines = [f"Found {len(invoices)} invoice(s):"]
        for inv in invoices:
            lines.append(
                f"  • *{inv.get('invoice_number','?')}* (ID: {inv.get('invoice_id','?')}) — "
                f"Date: {inv.get('date','?')} | "
                f"Due: {inv.get('due_date','?')} | "
                f"Status: {inv.get('status','?')} | "
                f"Total: *{inv.get('total','?')} {inv.get('currency_code','EGP')}*"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Zoho get invoices error: {e}"


def execute_zoho_get_invoice_detail(invoice_id):
    """Get full details of a Zoho Books invoice."""
    if not ZOHO_ORG_ID or not ZOHO_ACCESS_TOKEN:
        return "Zoho Books is not configured (missing ZOHO_ORG_ID or ZOHO_ACCESS_TOKEN)."
    try:
        url = f"https://www.zohoapis.com/books/v3/invoices/{invoice_id}"
        params = {"organization_id": ZOHO_ORG_ID}
        resp = requests.get(url, headers=_zoho_headers(), params=params, timeout=15)
        data = resp.json()
        inv = data.get("invoice", {})
        if not inv:
            return f"Invoice {invoice_id} not found."

        lines = [
            f"*Invoice {inv.get('invoice_number','?')}*",
            f"Client: *{inv.get('customer_name','?')}*",
            f"Date: {inv.get('date','?')} | Due: {inv.get('due_date','?')}",
            f"Status: *{inv.get('status','?')}*",
            "",
            "*Line Items:*"
        ]
        for item in inv.get("line_items", []):
            lines.append(
                f"  • {item.get('name') or item.get('description','?')} — "
                f"Qty: {item.get('quantity','?')} × "
                f"{item.get('rate','?')} = "
                f"*{item.get('item_total','?')}*"
            )
        lines += [
            "",
            f"Sub-total: {inv.get('sub_total','?')}",
            f"Tax: {inv.get('tax_total','?')}",
            f"*Total: {inv.get('total','?')} {inv.get('currency_code','EGP')}*",
            f"Balance Due: *{inv.get('balance','?')}*"
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"Zoho get invoice detail error: {e}"


def execute_zoho_send_invoice_to_slack(recipient_name, invoice_id, extra_note=""):
    """Fetch an invoice from Zoho Books and send a summary to a team member on Slack."""
    detail = execute_zoho_get_invoice_detail(invoice_id)
    if detail.startswith("Zoho") or detail.startswith("Invoice") and "not found" in detail:
        return detail  # propagate error
    message_parts = ["Here's the invoice info you asked about 📄", "", detail]
    if extra_note:
        message_parts += ["", f"_{extra_note}_"]
    message = "\n".join(message_parts)
    return execute_send_dm(recipient_name, message)


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

TOOL_DISPATCH = {
    "send_dm":                    lambda inp: execute_send_dm(
                                      inp["recipient_name"], inp["message"]),
    "gather_and_report_payments": lambda inp: execute_gather_and_report_payments(
                                      inp.get("note", "")),
    "zoho_search_client":         lambda inp: execute_zoho_search_client(
                                      inp["client_name"]),
    "zoho_get_invoices":          lambda inp: execute_zoho_get_invoices(
                                      inp["contact_id"], inp.get("status", "all")),
    "zoho_get_invoice_detail":    lambda inp: execute_zoho_get_invoice_detail(
                                      inp["invoice_id"]),
    "zoho_send_invoice_to_slack": lambda inp: execute_zoho_send_invoice_to_slack(
                                      inp["recipient_name"], inp["invoice_id"],
                                      inp.get("extra_note", "")),
}


def call_fengo(user_id, user_message, channel_id=None):
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    enriched = user_message
    keywords = ["introduce", "summarise", "summary", "payment", "outstanding",
                "otp", "pending", "confirm", "hello", "requests",
                "gather", "compile", "report", "zoho", "invoice", "client"]
    if any(kw in user_message.lower() for kw in keywords) and channel_id:
        history = get_channel_history(channel_id)
        enriched = (
            f"User asked: {user_message}\n\n"
            f"Channel history:\n===\n{history}\n===\n\n"
            f"Respond as Fengo."
        )

    conversation_histories[user_id].append({"role": "user", "content": enriched})

    # Agentic loop — handle tool use
    while True:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=conversation_histories[user_id][-20:]
        )

        # Append assistant turn
        conversation_histories[user_id].append({
            "role": "assistant",
            "content": response.content
        })

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    handler = TOOL_DISPATCH.get(block.name)
                    if handler:
                        result_text = handler(block.input)
                    else:
                        result_text = f"Unknown tool: {block.name}"
                    tool_results.append({
                        "type": "tool_use_id" if False else "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text
                    })

            conversation_histories[user_id].append({
                "role": "user",
                "content": tool_results
            })
            continue  # Loop again to get final reply

        # stop_reason == "end_turn" — extract text reply
        for block in response.content:
            if hasattr(block, "text"):
                return block.text
        return ""


# ---------------------------------------------------------------------------
# Slack event handlers
# ---------------------------------------------------------------------------

@app.event("app_mention")
def handle_mention(event, say, client):
    user_id = event["user"]
    channel = event["channel"]
    clean = re.sub(r"<@[A-Z0-9]+>", "", event["text"]).strip()
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
    channel_type = event.get("channel_type")
    if channel_type not in ("im", "mpim"):
        return
    if event.get("bot_id"):
        return
    user_id  = event["user"]
    channel_id = event["channel"]
    text = event.get("text", "").strip()
    if not text:
        return
    say(text=call_fengo(user_id, text, channel_id=channel_id))


scheduler = BackgroundScheduler(timezone="Africa/Cairo")
scheduler.start()

if __name__ == "__main__":
    print("Fengo is online.")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
