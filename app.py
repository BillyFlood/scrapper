import os
import uuid
import base64
import json
import logging
import stripe
from datetime import datetime
from flask import Flask, request, render_template, Response, stream_with_context, redirect
import anthropic
from pypdf import PdfReader
from pathlib import Path

# ── App setup ──

app = Flask(**name**, template_folder=os.path.join(os.path.dirname(**file**), ‘Templates’))
UPLOAD_FOLDER = ‘uploads’
LOG_FOLDER = ‘logs’
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

# ── Logging setup ──

# Writes every session to logs/audits.jsonl (one JSON object per line)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(**name**)

def log_session(session_id, event, data):
“”“Append a log entry to logs/audits.jsonl”””
entry = {
“timestamp”: datetime.utcnow().isoformat(),
“session_id”: session_id,
“event”: event,
**data
}
log_path = Path(LOG_FOLDER) / “audits.jsonl”
with open(log_path, “a”) as f:
f.write(json.dumps(entry) + “\n”)

# ── In-memory job store ──

# Requires workers=1 on gunicorn

_jobs = {}

# ── Anthropic client ──

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY automatically

# ── Stripe ──

stripe.api_key = os.environ.get(‘STRIPE_SECRET_KEY’, ‘’)
STRIPE_PRICE_ID = os.environ.get(‘STRIPE_PRICE_ID’, ‘’)        # create a $49 one-time price in Stripe dashboard
STRIPE_WEBHOOK_SECRET = os.environ.get(‘STRIPE_WEBHOOK_SECRET’, ‘’)

# ── Promo codes (comma-separated in env var, e.g. “BETA2025,TESTER1,BILLY”) ──

def get_promo_codes():
raw = os.environ.get(‘PROMO_CODES’, ‘WASTEHOUND_BETA’)
return [c.strip().upper() for c in raw.split(’,’) if c.strip()]

def is_valid_promo(code):
return code.upper() in get_promo_codes()

# ── Helpers ──

def extract_text_from_pdf(filepath):
try:
reader = PdfReader(filepath)
text = “”
for page in reader.pages:
text += page.extract_text()
return text.strip()
except Exception as e:
return f”[Could not extract PDF text: {e}]”

def encode_image_to_base64(filepath):
with open(filepath, “rb”) as f:
return base64.standard_b64encode(f.read()).decode(“utf-8”)

def _parse_containers(json_str):
“”“Parse containers_json into a readable string for Claude.”””
import json as _json
try:
containers = _json.loads(json_str or ‘[]’)
if not containers:
return ‘Not provided’
parts = []
for i, c in enumerate(containers, 1):
size = c.get(‘size’, ‘’)
freq = c.get(‘freq’, ‘’)
if size or freq:
parts.append(f”Container {i}: {size or ‘unknown size’}, {freq or ‘unknown frequency’}”)
return ’\n  ’.join(parts) if parts else ‘Not provided’
except Exception:
return ‘Not provided’

def build_user_data(form_data):
“”“Map lean form fields into a structured <user_data> block.”””
return f”””<user_data>

<BUSINESS_PROFILE>
Business Name: {form_data.get(‘business_name’, ‘Not provided’)}
Business Type: {form_data.get(‘business_type’, ‘Not provided’)}
Location: {form_data.get(‘location’, ‘Not provided’)}
</BUSINESS_PROFILE>

<WASTE_SERVICE_SETUP>
Monthly Spend: ${form_data.get(‘monthly_spend’, ‘Not provided’)} – THIS IS THE HARD CEILING FOR ALL SAVINGS ESTIMATES COMBINED
Waste Hauler: {form_data.get(‘waste_hauler’, ‘Not provided’)}
Containers: {_parse_containers(form_data.get(‘containers_json’, ‘[]’))}
</WASTE_SERVICE_SETUP>

<CONTRACT_INFO>
Contract End/Renewal: {form_data.get(‘contract_end_date’, ‘Not provided’)}
</CONTRACT_INFO>

<ADDITIONAL_NOTES>
{form_data.get(‘additional_notes’, ‘None provided’)}
</ADDITIONAL_NOTES>

</user_data>”””

def build_message_content(user_data_block, invoice_path, invoice_filename):
“”“Build the message content array.”””
message_content = []

```
if invoice_path and os.path.exists(invoice_path):
    ext = os.path.splitext(invoice_filename)[1].lower()
    if ext == '.pdf':
        pdf_text = extract_text_from_pdf(invoice_path)
        message_content.append({
            "type": "text",
            "text": f"Here is the extracted text from the customer's waste invoice:\n\n---\n{pdf_text}\n---"
        })
    elif ext in ['.jpg', '.jpeg', '.png']:
        image_data = encode_image_to_base64(invoice_path)
        media_type = "image/jpeg" if ext in ['.jpg', '.jpeg'] else "image/png"
        message_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_data
            }
        })
        message_content.append({
            "type": "text",
            "text": "Above is the customer's waste invoice image."
        })

message_content.append({
    "type": "text",
    "text": f"{user_data_block}\n\nPlease produce a full WasteHound audit report based on the invoice and business information provided above."
})

return message_content
```

SYSTEM_PROMPT = “”“You are WasteHound, an expert waste management consultant with 20 years of experience auditing commercial waste invoices and operations for small and medium businesses across the United States.

Your reports are punchy, specific, and immediately actionable. You lead with findings. Every issue gets a dollar amount. Every recommendation names a specific vendor, program, or action.

Your analysis covers TWO dimensions:

1. DISPOSAL OPTIMIZATION – Billing errors, right-sizing, diversion savings, contract issues.
1. WASTE REDUCTION – Can they generate less waste? Identify specific upstream changes – procurement swaps, supplier take-back programs, reusable alternatives, inventory practices – that shrink the waste stream before it hits the dumpster.

CRITICAL RULES:

- Always put dollar amounts on findings and recommendations
- Always name specific local vendors, composters, recyclers, and programs for the business’s city/region
- If you don’t know exact local rates, give a realistic market range for that region
- Quantify GHG impact in plain terms (e.g. “equivalent to removing X cars from the road”)
- Be direct. If something is wrong, say it plainly.
- Lead with reduction, then diversion, then disposal optimization – in that priority order
- Estimate volume reduction percentages and translate to hauling frequency reductions and dollar savings
- Name specific products, suppliers, or programs – not generic advice

FINANCIAL ACCURACY RULES – these are non-negotiable:

- The total of ALL finding amounts combined must NEVER exceed the user’s stated monthly spend
- Each individual finding’s dollar impact must be a realistic fraction of the total bill – not the entire bill
- Before writing the SCORES line, mentally sum all finding amounts to verify the total is less than monthly spend
- If the user pays $450/month, you cannot find more than $450/month in savings – realistically, findings should total 15-35% of spend in most cases
- Dollar amounts must be grounded in the actual data provided – if no invoice was uploaded, state that estimates are based on typical rates for the business type and region
- Never fabricate specific line items (e.g. exact surcharge percentages) if no invoice was provided – instead use ranges and mark as estimated
- If monthly spend is not provided or seems implausible, flag this and reduce confidence to Low

NO DOUBLE-COUNTING RULES – critical for savings integrity:

- Savings estimates must reflect a realistic chain of sequential steps, not a stack of independent siloed estimates that all draw from the same cost base
- Waste reduction and disposal cost savings CANNOT both be counted in full – if waste reduction shrinks volume by 30%, then disposal cost savings must be calculated on the reduced volume, not the original volume
- Container downsizing and pickup frequency reduction CANNOT both be counted independently – they share the same service cost base; pick the dominant saving and note the other as additional upside
- If you identify multiple paths to savings (e.g. source reduction vs. contract renegotiation), present them as ALTERNATIVE SCENARIOS, not additive. Label them clearly: “Path A: Source Reduction (~$X/mo)” and “Path B: Contract Renegotiation (~$Y/mo)” – the user pursues one path, not both simultaneously
- The only savings that can be genuinely stacked are those that target completely different cost lines – for example, a fuel surcharge overcharge (billing line item) can be stacked with a cardboard diversion saving (separate service), because they do not share a cost base
- When in doubt, underestimate. A conservative finding that turns out to be right builds trust. An inflated finding that the business owner can’t achieve destroys it.
- In the Action Plan, make the sequencing and dependencies explicit: “Do step 1 before step 2 – step 2 savings assume step 1 is already in place.”

FORMAT RULES:

- Every finding uses the finding-card + finding-detail + detail-row structure – NO prose paragraphs in findings
- Detail labels are ≤4 words
- Detail values are ≤2 sentences (except action-row, which may be 3)
- finding-card severity: red = billing errors/overcharges, amber = inefficiencies, green = opportunities
- data-impact attribute = monthly dollar impact as integer
- Action plan items use action-item structure, NOT <ol>/<li>
- Priority classes: p1 = highest impact, p2 = medium, p3 = lower
- Do NOT write “It’s worth noting” or “Additionally” or “In conclusion”
- Lead every detail-value with the number or fact first, then context

Format your response as follows:

FIRST LINE – output this before any HTML (mandatory):

<!--SCORES:{"grade":"C+","savings":847,"issues":4,"diversion":32,"confidence":"Medium","headline":"One punchy sentence summarising the headline finding."}-->

The “savings” value in the SCORES line must equal the realistic sum of all finding amounts combined – and must be less than the user’s stated monthly spend. If a user pays $450/month, the savings value must be well under $450. A typical well-run audit finds 15-35% of monthly spend in recoverable savings. Finding 80%+ of spend in savings is a red flag that you have fabricated or inflated figures – review and reduce before outputting.

Then output the full HTML report:

<div class="audit-report">

  <div class="audit-section findings-section">
    <h2>Findings <span class="section-count">[N] issues flagged</span></h2>

```
<div class="finding-card red" data-impact="87">
  <div class="finding-header">
    <span class="finding-badge">BILLING ERROR</span>
    <span class="finding-title">[Short title]</span>
    <span class="finding-amount">-$[amount]/mo</span>
  </div>
  <div class="finding-detail">
    <div class="detail-row">
      <span class="detail-label">Invoice shows</span>
      <span class="detail-value">[Specific line item and amount from their invoice]</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Should be</span>
      <span class="detail-value">[Market rate or correct amount with source]</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Overcharge</span>
      <span class="detail-value red">$[amount]/mo ($[amount x12]/yr)</span>
    </div>
    <div class="detail-row action-row">
      <span class="detail-label">Fix it</span>
      <span class="detail-value">[Exactly who to call, what to say, what to reference]</span>
    </div>
  </div>
</div>
<!-- Repeat finding-card for each finding -->
```

  </div>

  <div class="audit-section reduction-section">
    <h2>Waste Reduction</h2>
    <p class="section-intro">Changes that shrink your waste stream at the source -- fewer tons generated means fewer tons to haul.</p>

```
<div class="finding-card green" data-impact="120">
  <div class="finding-header">
    <span class="finding-badge">REDUCTION</span>
    <span class="finding-title">[Short title]</span>
    <span class="finding-amount">-$[amount]/mo</span>
  </div>
  <div class="finding-detail">
    <div class="detail-row">
      <span class="detail-label">Current waste</span>
      <span class="detail-value">[Material, volume, why it exists]</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Change to</span>
      <span class="detail-value">[Specific product, supplier, or program -- named]</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Impact</span>
      <span class="detail-value green">[Volume reduction → hauling reduction → dollar savings]</span>
    </div>
    <div class="detail-row action-row">
      <span class="detail-label">First step</span>
      <span class="detail-value">[Who to contact, what to request]</span>
    </div>
  </div>
</div>
```

  </div>

  <div class="audit-section diversion-section">
    <h2>Diversion &amp; Sustainability</h2>
    <p class="section-intro">Local programs to divert waste from landfill -- with net cost or savings for each.</p>
    <!-- Same finding-card green structure -->
  </div>

  <div class="audit-section action-plan-section">
    <h2>Action Plan</h2>
    <p class="section-intro">Ranked by impact. Reduction first, then optimization. Do #1 first.</p>

```
<div class="action-item">
  <div class="action-priority p1">1</div>
  <div class="action-content">
    <div class="action-title">[Action title]</div>
    <div class="action-meta">
      <span class="action-savings">Saves ~$[X]/mo</span>
      <span class="action-effort">[e.g. "1 phone call" or "Contract negotiation"]</span>
    </div>
    <div class="action-desc">[One sentence: what to do, who to contact, when.]</div>
  </div>
</div>
<!-- Repeat action-item, incrementing priority number. Use p1/p2/p3/p4 classes. -->
```

  </div>

  <div class="audit-section contract-section">
    <h2>Contract Watch</h2>
    <div class="detail-row">
      <span class="detail-label">Renewal date</span>
      <span class="detail-value">[Date and advice]</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Auto-renewal</span>
      <span class="detail-value">[Clause details and risk]</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Rate lock</span>
      <span class="detail-value">[Whether rates are fixed and for how long]</span>
    </div>
    <div class="detail-row action-row">
      <span class="detail-label">Action</span>
      <span class="detail-value">[Specific negotiation advice or next step]</span>
    </div>
  </div>

  <div class="audit-section compliance-section">
    <h2>Regulatory Compliance</h2>
    <!-- detail-row structure for each relevant regulation -->
    <div class="detail-row">
      <span class="detail-label">[Regulation name]</span>
      <span class="detail-value">[Status: compliant / at risk / non-compliant + what to do]</span>
    </div>
  </div>

</div>

CRITICAL FORMAT RULES – read these carefully:

- The FIRST LINE must be the <!--SCORES:--> comment. No exceptions.
- Every finding-card must have a finding-badge, finding-title, finding-amount, and finding-detail
- detail-row labels are UPPERCASE, ≤4 words
- Do NOT put free text or paragraphs inside finding-detail – only detail-row elements
- action-item elements use the action-priority/action-content/action-title/action-meta/action-desc structure
- Priority number must match the <div class="action-priority p1">1</div> pattern – p1 for first, p2 for second, p3 for third, p4 for fourth and beyond
- Do NOT use <ol> or <li> anywhere in the output
- No inline styles. No <style> blocks. Only the class names defined above.
- Do NOT add sections not listed above.”””

# ── Routes ──

@app.route(’/’)
def index():
return render_template(‘index.html’)

@app.route(’/audit’)
def audit_form():
return render_template(‘audit_form.html’)

@app.route(’/submit’, methods=[‘POST’])
def submit():
session_id = str(uuid.uuid4())[:8]

```
form_data = {
    'business_name':    request.form.get('business_name', '').strip(),
    'business_type':    request.form.get('business_type', ''),
    'location':         request.form.get('location', ''),
    'monthly_spend':    request.form.get('monthly_spend', ''),
    'email':            request.form.get('email', '').strip(),
    # Optional accordion fields
    'waste_hauler':     request.form.get('waste_hauler', ''),
    'containers_json':  request.form.get('containers_json', '[]'),
    'contract_end_date':request.form.get('contract_end_date', ''),
    'additional_notes': request.form.get('additional_notes', ''),
    'promo_code':      request.form.get('promo_code', '').strip(),
}

# Handle invoice upload
invoice_file = request.files.get('invoice')
invoice_path = None
invoice_filename = None
if invoice_file and invoice_file.filename != '':
    invoice_filename = invoice_file.filename
    invoice_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_{invoice_filename}")
    invoice_file.save(invoice_path)

# Log the incoming submission
logger.info(f"[{session_id}] New audit: {form_data.get('business_name') or form_data['business_type']} | {form_data['location']} | ${form_data['monthly_spend']}/mo | email: {form_data.get('email') or 'none'}")
log_session(session_id, "submission", {
    "business_type": form_data['business_type'],
    "location": form_data['location'],
    "monthly_spend": form_data['monthly_spend'],
    "waste_hauler": form_data.get('waste_hauler', ''),
    "email": form_data.get('email', ''),
    "invoice_uploaded": invoice_filename is not None,
    "form_data": form_data
})

# Build Claude payload and store in job queue
user_data_block = build_user_data(form_data)
message_content = build_message_content(user_data_block, invoice_path, invoice_filename)

token = str(uuid.uuid4())
_jobs[token] = {
    "session_id": session_id,
    "business_name": form_data.get('business_name') or (form_data.get('business_type', 'Business') + ' · ' + form_data.get('location', '')),
    "message_content": message_content
}

business_name = form_data.get('business_name', '').strip()
biz_type = form_data.get('business_type', '')
location = form_data.get('location', '')
if business_name:
    display_name = business_name
elif location:
    display_name = f"{biz_type} · {location}"
else:
    display_name = biz_type or 'Your Business'

# ── Check promo code ──
promo = form_data.get('promo_code', '').strip()
if promo and is_valid_promo(promo):
    logger.info(f"[{session_id}] Promo code accepted: {promo}")
    log_session(session_id, "promo_used", {"promo_code": promo})
    return render_template('results.html',
                           business_name=display_name,
                           token=token,
                           email_provided=bool(form_data.get('email', '')))

# ── Gate behind Stripe payment ──
try:
    checkout = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{
            'price': STRIPE_PRICE_ID,
            'quantity': 1,
        }],
        mode='payment',
        success_url=request.host_url + f'results/{token}?paid=1',
        cancel_url=request.host_url + 'audit?cancelled=1',
        metadata={'token': token, 'session_id': session_id},
        customer_email=form_data.get('email') or None,
    )
    log_session(session_id, "checkout_created", {"stripe_session": checkout.id})
    return redirect(checkout.url, code=303)
except Exception as e:
    logger.error(f"[{session_id}] Stripe error: {e}")
    return render_template('error.html', error_message=f"Payment setup failed: {str(e)}")
```

@app.route(’/stream/<token>’)
def stream(token):
“”“SSE endpoint – streams response chunk by chunk.”””

```
if token not in _jobs:
    def err():
        yield f"data: {json.dumps({'error': 'Session expired or not found. Please try again.'})}\n\n"
    return Response(stream_with_context(err()), mimetype='text/event-stream')

job = _jobs.pop(token)
session_id = job['session_id']
business_name = job['business_name']
message_content = job['message_content']

def generate():
    full_response = ""
    try:
        logger.info(f"[{session_id}] Starting stream for: {business_name}")

        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": message_content}]
        ) as stream:
            for text in stream.text_stream:
                full_response += text
                yield f"data: {json.dumps({'text': text})}\n\n"

        # Log completed response
        logger.info(f"[{session_id}] Stream complete. Response length: {len(full_response)} chars")
        log_session(session_id, "completion", {
            "business_name": business_name,
            "response_length": len(full_response),
            "response_html": full_response
        })

        yield f"data: {json.dumps({'done': True})}\n\n"

    except Exception as e:
        error_msg = str(e)
        logger.error(f"[{session_id}] Stream error: {error_msg}")
        log_session(session_id, "error", {
            "business_name": business_name,
            "error": error_msg
        })
        yield f"data: {json.dumps({'error': error_msg})}\n\n"

return Response(stream_with_context(generate()), mimetype='text/event-stream')
```

@app.route(’/capture-email’, methods=[‘POST’])
def capture_email():
“”“Store email from post-results nudge.”””
from flask import jsonify
data = request.get_json()
email = data.get(‘email’, ‘’).strip()
token = data.get(‘token’, ‘’)
source = data.get(‘source’, ‘nudge’)
if email:
logger.info(f”Email captured via {source}: {email}”)
log_session(token[:8] if token else ‘unknown’, ‘email_capture’, {
“email”: email,
“source”: source
})
return jsonify({“ok”: True})

@app.route(’/stripe-webhook’, methods=[‘POST’])
def stripe_webhook():
“”“Handle Stripe payment confirmation events.”””
payload = request.get_data()
sig_header = request.headers.get(‘Stripe-Signature’, ‘’)
try:
if STRIPE_WEBHOOK_SECRET:
event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
else:
event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)

```
    if event['type'] == 'checkout.session.completed':
        session_data = event['data']['object']
        token = session_data.get('metadata', {}).get('token', '')
        sid = session_data.get('metadata', {}).get('session_id', '')
        logger.info(f"[{sid}] Payment confirmed for token: {token}")
        log_session(sid, "payment_confirmed", {
            "stripe_session": session_data.get('id'),
            "amount": session_data.get('amount_total'),
            "customer_email": session_data.get('customer_email'),
        })
except Exception as e:
    logger.error(f"Webhook error: {e}")
    return 'error', 400

return 'ok', 200
```

# ── Admin page ──

ADMIN_PASSWORD = os.environ.get(“ADMIN_PASSWORD”, “wastehound2025”)

@app.route(’/admin’)
def admin():
password = request.args.get(‘pw’, ‘’)
if password != ADMIN_PASSWORD:
return “””
<html><body style="font-family:sans-serif;padding:3rem;background:#fafaf7">
<h2>WasteHound Admin</h2>
<form>
<input type="password" name="pw" placeholder="Password" 
style="padding:.5rem;border:1px solid #ccc;border-radius:6px;margin-right:.5rem"/>
<button type="submit" 
style="padding:.5rem 1rem;background:#1a2e18;color:white;border:none;border-radius:6px;cursor:pointer">
Enter
</button>
</form>
</body></html>
“””

```
# Read and parse the log file
log_path = Path(LOG_FOLDER) / "audits.jsonl"
sessions = {}

if log_path.exists():
    with open(log_path, "r") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                sid = entry.get("session_id", "unknown")
                if sid not in sessions:
                    sessions[sid] = {}
                sessions[sid][entry["event"]] = entry
            except:
                continue

# Build admin HTML
rows = ""
for sid, events in reversed(list(sessions.items())):
    sub = events.get("submission", {})
    comp = events.get("completion", {})
    err = events.get("error", {})

    status = "✅ Complete" if comp else ("❌ Error" if err else "⏳ In Progress")
    timestamp = sub.get("timestamp", "")[:16].replace("T", " ")
    business = sub.get("business_name", "Unknown")
    btype = sub.get("business_type", "")
    location = sub.get("location", "")
    spend = sub.get("monthly_spend", "")
    hauler = sub.get("waste_hauler", "")
    invoice = "Yes" if sub.get("invoice_uploaded") else "No"
    email_capture = events.get("email_capture", {})
    email = sub.get("email") or email_capture.get("email") or ""
    response_len = comp.get("response_length", err.get("error", ""))
    response_html = comp.get("response_html", "")

    rows += f"""
    <tr style="border-bottom:1px solid #eee">
        <td style="padding:.8rem;white-space:nowrap">{timestamp}</td>
        <td style="padding:.8rem"><strong>{business}</strong><br/>
            <small style="color:#888">{btype} · {location}</small></td>
        <td style="padding:.8rem">${spend}/mo<br/><small style="color:#888">{hauler}</small></td>
        <td style="padding:.8rem">{email or "<span style='color:#bbb'>--</span>"}</td>
        <td style="padding:.8rem">{invoice}</td>
        <td style="padding:.8rem">{status}</td>
        <td style="padding:.8rem">
            <button onclick="document.getElementById('resp-{sid}').style.display='block'"
                    style="font-size:.75rem;padding:.3rem .7rem;background:#1a2e18;color:white;
                           border:none;border-radius:4px;cursor:pointer">
                View Report
            </button>
            <div id="resp-{sid}" style="display:none;margin-top:1rem;padding:1rem;
                 background:#f8f8f5;border:1px solid #ddd;border-radius:6px;
                 max-height:400px;overflow-y:auto;font-size:.8rem">
                {response_html or err.get("error", "No response recorded")}
            </div>
        </td>
    </tr>"""

total = len(sessions)
complete = sum(1 for s in sessions.values() if "completion" in s)
errors = sum(1 for s in sessions.values() if "error" in s)

return f"""
<html>
<head>
    <title>WasteHound Admin</title>
    <style>
        body {{ font-family: 'DM Sans', sans-serif; background: #fafaf7; color: #1a1a18; margin: 0; }}
        nav {{ background: #1a2e18; padding: 1rem 2rem; color: white; display:flex; justify-content:space-between; align-items:center; }}
        nav h1 {{ font-size: 1.1rem; margin:0; }}
        .stats {{ display:flex; gap:1.5rem; padding:1.5rem 2rem; border-bottom:1px solid #eee; }}
        .stat {{ background:white; border:1px solid #eee; border-radius:8px; padding:1rem 1.5rem; text-align:center; }}
        .stat-num {{ font-size:1.8rem; font-weight:600; color:#1a2e18; }}
        .stat-label {{ font-size:.75rem; color:#888; text-transform:uppercase; letter-spacing:.06em; }}
        table {{ width:100%; border-collapse:collapse; background:white; }}
        th {{ font-size:.7rem; text-transform:uppercase; letter-spacing:.08em; color:#888;
              padding:.8rem; text-align:left; background:#fafaf7; border-bottom:2px solid #eee; }}
        .wrap {{ padding: 1.5rem 2rem; }}
    </style>
</head>
<body>
    <nav>
        <h1>WasteHound -- Audit Log</h1>
        <span style="font-size:.82rem;opacity:.6">{total} total sessions</span>
    </nav>
    <div class="stats">
        <div class="stat"><div class="stat-num">{total}</div><div class="stat-label">Total Audits</div></div>
        <div class="stat"><div class="stat-num">{complete}</div><div class="stat-label">Completed</div></div>
        <div class="stat"><div class="stat-num">{errors}</div><div class="stat-label">Errors</div></div>
    </div>
    <div class="wrap">
    <table>
        <thead>
            <tr>
                <th>Time</th>
                <th>Business</th>
                <th>Spend</th>
                <th>Invoice</th>
                <th>Status</th>
                <th>Report</th>
            </tr>
        </thead>
        <tbody>
            {rows if rows else '<tr><td colspan="6" style="padding:2rem;text-align:center;color:#aaa">No audits yet</td></tr>'}
        </tbody>
    </table>
    </div>
</body>
</html>
"""
```

if **name** == ‘**main**’:
app.run(debug=True)
