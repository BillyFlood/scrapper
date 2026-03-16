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
app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
UPLOAD_FOLDER = 'uploads'
LOG_FOLDER = 'logs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

# ── Logging setup ──
# Writes every session to logs/audits.jsonl (one JSON object per line)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def log_session(session_id, event, data):
    """Append a log entry to logs/audits.jsonl"""
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "session_id": session_id,
        "event": event,
        **data
    }
    log_path = Path(LOG_FOLDER) / "audits.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

# ── In-memory job store ──
# Requires workers=1 on gunicorn
_jobs = {}

# ── Anthropic client ──
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY automatically

# ── Stripe ──
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID', '')        # create a $49 one-time price in Stripe dashboard
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

# ── Promo codes (comma-separated in env var, e.g. "BETA2025,TESTER1,BILLY") ──
def get_promo_codes():
    raw = os.environ.get('PROMO_CODES', 'WASTEHOUND_BETA')
    return [c.strip().upper() for c in raw.split(',') if c.strip()]

def is_valid_promo(code):
    return code.upper() in get_promo_codes()


# ── Helpers ──
def extract_text_from_pdf(filepath):
    try:
        reader = PdfReader(filepath)
        text = ""
        for page in reader.pages:
            text += page.extract_text()
        return text.strip()
    except Exception as e:
        return f"[Could not extract PDF text: {e}]"


def encode_image_to_base64(filepath):
    with open(filepath, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def _parse_containers(json_str):
    """Parse containers_json into a readable string for Claude."""
    import json as _json
    try:
        containers = _json.loads(json_str or '[]')
        if not containers:
            return 'Not provided'
        parts = []
        for i, c in enumerate(containers, 1):
            size = c.get('size', '')
            freq = c.get('freq', '')
            if size or freq:
                parts.append(f"Container {i}: {size or 'unknown size'}, {freq or 'unknown frequency'}")
        return '\n  '.join(parts) if parts else 'Not provided'
    except Exception:
        return 'Not provided'


def build_user_data(form_data):
    """Map lean form fields into a structured <user_data> block."""
    return f"""<user_data>

<BUSINESS_PROFILE>
  Business Type: {form_data.get('business_type', 'Not provided')}
  Location: {form_data.get('location', 'Not provided')}
</BUSINESS_PROFILE>

<WASTE_SERVICE_SETUP>
  Monthly Spend: ${form_data.get('monthly_spend', 'Not provided')}
  Waste Hauler: {form_data.get('waste_hauler', 'Not provided')}
  Containers: {_parse_containers(form_data.get('containers_json', '[]'))}
</WASTE_SERVICE_SETUP>

<CONTRACT_INFO>
  Contract End/Renewal: {form_data.get('contract_end_date', 'Not provided')}
</CONTRACT_INFO>

<ADDITIONAL_NOTES>
{form_data.get('additional_notes', 'None provided')}
</ADDITIONAL_NOTES>

</user_data>"""


def build_message_content(user_data_block, invoice_path, invoice_filename):
    """Build the message content array."""
    message_content = []

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


SYSTEM_PROMPT = """You are WasteHound, an expert waste management consultant with 20 years of experience auditing commercial waste invoices and operations for small and medium businesses across the United States.

Your audit reports are known for being punchy, specific, and immediately actionable. You do not write essays. You lead with the findings. Every issue gets a dollar amount. Every recommendation names a specific vendor, program, or action — not a generic suggestion.

Your analysis covers TWO dimensions:
1. DISPOSAL OPTIMIZATION — Are they paying too much to haul and process the waste they generate? Are there billing errors, right-sizing opportunities, diversion savings, or contract issues?
2. WASTE REDUCTION — Can they generate less waste in the first place? Fewer tons generated means fewer tons to pay for. Analyze the business type, industry, and any details provided to identify specific upstream changes — procurement swaps, supplier take-back programs, reusable alternatives, inventory practices, packaging negotiations, portion/material controls — that would shrink the waste stream before it ever hits the dumpster.

CRITICAL RULES:
- Never write long paragraphs where a structured finding will do
- Always put dollar amounts on findings and recommendations
- Always name specific local vendors, composters, recyclers, and programs relevant to the business's city/region
- If you don't know the exact rate for a local vendor, give a realistic market range for that region
- Quantify GHG/environmental impact in plain terms (e.g. "equivalent to removing X cars from the road")
- Be direct. If something is wrong, say it plainly. Do not soften findings.
- For every waste stream identified, ask: "Can this be reduced or eliminated at the source BEFORE optimizing its disposal?" Lead with reduction, then diversion, then disposal optimization — in that priority order.
- Estimate volume reduction percentages and translate them into hauling frequency reductions and dollar savings (e.g. "Switching to reusable shipping totes eliminates ~2 cubic yards/week of cardboard → drop from 2x/week pickup to 1x/week → saves $X/mo")
- Name specific products, suppliers, or programs for reduction recommendations — not generic advice like "reduce packaging"

Format your response in clean HTML rendered directly in a web page. Use EXACTLY this structure:

<div class="audit-report">

  <div class="audit-section summary-section">
    <h2>Audit Summary</h2>
    <p class="summary-intro">[One punchy sentence: the headline finding for this business.]</p>
    <div class="metrics-row">
      <div class="metric-box">
        <div class="metric-label">Est. Monthly Savings</div>
        <div class="metric-value green">$[amount]</div>
      </div>
      <div class="metric-box">
        <div class="metric-label">Issues Found</div>
        <div class="metric-value amber">[number]</div>
      </div>
      <div class="metric-box">
        <div class="metric-label">Efficiency Grade</div>
        <div class="metric-value">[A/B/C/D/F]</div>
      </div>
      <div class="metric-box">
        <div class="metric-label">Reduction Potential</div>
        <div class="metric-value">[X]%</div>
      </div>
      <div class="metric-box">
        <div class="metric-label">Confidence</div>
        <div class="metric-value">[High/Medium/Low]</div>
      </div>
    </div>
  </div>

  <div class="audit-section findings-section">
    <h2>Findings</h2>
    <div class="finding-card red">
      <div class="finding-header">
        <span class="finding-title">[Short title e.g. "Fuel Surcharge Overcharge"]</span>
        <span class="finding-amount">-$[monthly impact]</span>
      </div>
      <div class="finding-body">
        <strong>What we found:</strong> [Specific to their invoice. Name the exact line item and why it's wrong.]<br/>
        <strong>What it should be:</strong> [The correct amount or industry standard.]<br/>
        <strong>Action:</strong> [Exactly what to do — who to call, what to say.]
      </div>
    </div>
  </div>

  <div class="audit-section reduction-section">
    <h2>Waste Reduction Opportunities</h2>
    <p class="section-intro">These changes shrink your waste stream at the source — fewer tons generated means fewer tons to haul and pay for.</p>
    <div class="finding-card green">
      <div class="finding-header">
        <span class="finding-title">[Short title e.g. "Switch to Reusable Produce Crates"]</span>
        <span class="finding-amount">-[X] cubic yards/week</span>
      </div>
      <div class="finding-body">
        <strong>Current waste generated:</strong> [What material, how much, why it exists — tied to a specific business practice or procurement choice.]<br/>
        <strong>Reduction strategy:</strong> [Specific change — name exact products, suppliers, or programs. E.g. "Switch from single-use waxed cardboard produce boxes to IFCO reusable plastic crates through their RPCs program — your Sysco rep can set this up."]<br/>
        <strong>Volume impact:</strong> [Estimated reduction in cubic yards or tons per week/month.]<br/>
        <strong>Cost impact:</strong> [Net savings after any new costs. Tie to hauling frequency reduction, container downsizing, or both. E.g. "Eliminates ~3 CY/week of cardboard waste → downsize from 6 CY dumpster to 4 CY → saves $[X]/mo in hauling fees, minus $[Y]/mo crate program cost = net $[Z]/mo."]<br/>
        <strong>Action:</strong> [Exactly what to do — who to contact, what to request.]
      </div>
    </div>
  </div>

  <div class="audit-section">
    <h2>Disposal Cost Savings</h2>
    <ol class="savings-list">
      <li>
        <strong>[Opportunity title]</strong> — Est. savings: <strong class="green">$[amount]/mo</strong><br/>
        [2-3 sentences. Specific: what to change, who to contact, what the new cost would be.]
      </li>
    </ol>
  </div>

  <div class="audit-section">
    <h2>Diversion & Sustainability</h2>
    <ul class="diversion-list">
      <li>
        <strong>[Stream or opportunity]:</strong> [Name the actual local composter, MRF, or program. Include estimated cost or savings. Include GHG impact in plain language.]
      </li>
    </ul>
  </div>

  <div class="audit-section">
    <h2>Contract Watch</h2>
    <ul class="contract-list">
      <li><strong>[Contract issue]:</strong> [Specific, actionable advice.]</li>
    </ul>
  </div>

  <div class="audit-section action-plan-section">
    <h2>Your Action Plan</h2>
    <p>Ranked by impact. Reduction first, then optimization.</p>
    <ol class="action-list">
      <li>
        <strong>[Action title]</strong> <span class="action-impact">— saves ~$[amount]/mo</span><br/>
        [One sentence: exactly what to do, who to contact, when.]
      </li>
    </ol>
  </div>

</div>

HTML rules:
- Use the exact class names above — they control styling
- finding-card severity: red = billing errors, amber = inefficiencies, green = reduction opportunities or missed opportunities
- Use <strong> for dollar amounts, key terms, vendor names
- Use <span class="green"> for savings, <span class="red"> for overcharges
- No inline styles. No CSS. Just the HTML structure above.
- Do not add any sections not listed above."""


# ── Routes ──

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/audit')
def audit_form():
    return render_template('audit_form.html')


@app.route('/submit', methods=['POST'])
def submit():
    session_id = str(uuid.uuid4())[:8]

    form_data = {
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
    logger.info(f"[{session_id}] New audit: {form_data['business_type']} | {form_data['location']} | ${form_data['monthly_spend']}/mo | email: {form_data.get('email') or 'none'}")
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
        "business_name": form_data.get('business_type', 'Business') + ' · ' + form_data.get('location', ''),
        "message_content": message_content
    }

    display_name = f"{form_data['business_type']} · {form_data['location']}" if form_data.get('location') else form_data['business_type']

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


@app.route('/stream/<token>')
def stream(token):
    """SSE endpoint — streams response chunk by chunk."""

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



@app.route('/capture-email', methods=['POST'])
def capture_email():
    """Store email from post-results nudge."""
    from flask import jsonify
    data = request.get_json()
    email = data.get('email', '').strip()
    token = data.get('token', '')
    source = data.get('source', 'nudge')
    if email:
        logger.info(f"Email captured via {source}: {email}")
        log_session(token[:8] if token else 'unknown', 'email_capture', {
            "email": email,
            "source": source
        })
    return jsonify({"ok": True})



@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe payment confirmation events."""
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)

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


# ── Admin page ──
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "wastehound2025")

@app.route('/admin')
def admin():
    password = request.args.get('pw', '')
    if password != ADMIN_PASSWORD:
        return """
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
        """

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
            <td style="padding:.8rem">{email or "<span style='color:#bbb'>—</span>"}</td>
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
            <h1>WasteHound — Audit Log</h1>
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


if __name__ == '__main__':
    app.run(debug=True)
