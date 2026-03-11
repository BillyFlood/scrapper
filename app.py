import os
import uuid
import base64
import json
import logging
from datetime import datetime
from flask import Flask, request, render_template, Response, stream_with_context
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


def build_user_data(form_data):
    """Map form fields into a structured <user_data> block for Claude."""

    # Collect all waste stream rows
    stream_types = request.form.getlist('stream_type')
    container_types = request.form.getlist('container_type')
    container_sizes = request.form.getlist('container_size')
    container_counts = request.form.getlist('container_count')
    pickup_freqs = request.form.getlist('pickup_frequency')
    fill_levels = request.form.getlist('fill_level')
    compactions = request.form.getlist('compaction')

    streams_text = ""
    for i in range(len(stream_types)):
        streams_text += f"""
    Stream {i+1}:
      - Type: {stream_types[i] if i < len(stream_types) else 'N/A'}
      - Container: {container_types[i] if i < len(container_types) else 'N/A'}
      - Size: {container_sizes[i] if i < len(container_sizes) else 'N/A'}
      - Count: {container_counts[i] if i < len(container_counts) else 'N/A'}
      - Pickups/week: {pickup_freqs[i] if i < len(pickup_freqs) else 'N/A'}
      - Fill level: {fill_levels[i] if i < len(fill_levels) else 'N/A'}
      - Compacted: {compactions[i] if i < len(compactions) else 'N/A'}"""

    return f"""<user_data>

<BUSINESS_PROFILE>
  Business Name: {form_data.get('business_name', 'Not provided')}
  Business Type: {form_data.get('business_type', 'Not provided')}
  Location: {form_data.get('location', 'Not provided')}
  Employees: {form_data.get('employees', 'Not provided')}
  Square Footage: {form_data.get('square_footage', 'Not provided')}
  Operating Days/Week: {form_data.get('operating_days', 'Not provided')}
</BUSINESS_PROFILE>

<WASTE_SERVICE_SETUP>
  Waste Hauler: {form_data.get('waste_hauler', 'Not provided')}
  Monthly Spend: ${form_data.get('monthly_spend', 'Not provided')}
  Waste Streams:
{streams_text}
</WASTE_SERVICE_SETUP>

<CONTRACT_INFO>
  Contract Start: {form_data.get('contract_start_date', 'Not provided')}
  Contract End/Renewal: {form_data.get('contract_end_date', 'Not provided')}
  Auto-Renewal Clause: {form_data.get('auto_renewal', 'Not provided')}
  Early Termination Penalty: {form_data.get('early_termination', 'Not provided')}
  Rate Type: {form_data.get('rate_type', 'Not provided')}
</CONTRACT_INFO>

<WASTE_COMPOSITION>
  Top Waste Materials: {form_data.get('top_waste_materials', 'Not provided')}
  Contamination Issues: {form_data.get('contamination', 'Not provided')}
</WASTE_COMPOSITION>

<GOALS_AND_PREFERENCES>
  Primary Goal: {form_data.get('primary_goal', 'Not provided')}
  Open to Switching Haulers: {form_data.get('open_to_switching', 'Not provided')}
  Program Budget: {form_data.get('program_budget', 'Not provided')}
  Existing Initiatives: {form_data.get('existing_initiatives', 'Not provided')}
</GOALS_AND_PREFERENCES>

<ADDITIONAL_NOTES>
{form_data.get('additional_notes', 'None provided')}
</ADDITIONAL_NOTES>

</user_data>"""


def build_message_content(user_data_block, invoice_path, invoice_filename):
    """Build the Claude message content array."""
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

CRITICAL RULES:
- Never write long paragraphs where a structured finding will do
- Always put dollar amounts on findings and recommendations
- Always name specific local vendors, composters, recyclers, and programs relevant to the business's city/region
- If you don't know the exact rate for a local vendor, give a realistic market range for that region
- Quantify GHG/environmental impact in plain terms (e.g. "equivalent to removing X cars from the road")
- Be direct. If something is wrong, say it plainly. Do not soften findings.

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

  <div class="audit-section">
    <h2>Cost Saving Opportunities</h2>
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
    <p>Ranked by impact. Do these in order.</p>
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
- finding-card severity: red = billing errors, amber = inefficiencies, green = missed opportunities
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
        'business_name':       request.form.get('business_name', ''),
        'business_type':       request.form.get('business_type', ''),
        'location':            request.form.get('location', ''),
        'employees':           request.form.get('employees', ''),
        'square_footage':      request.form.get('square_footage', ''),
        'operating_days':      request.form.get('operating_days', ''),
        'waste_hauler':        request.form.get('waste_hauler', ''),
        'monthly_spend':       request.form.get('monthly_spend', ''),
        'contract_start_date': request.form.get('contract_start_date', ''),
        'contract_end_date':   request.form.get('contract_end_date', ''),
        'auto_renewal':        request.form.get('auto_renewal', ''),
        'early_termination':   request.form.get('early_termination', ''),
        'rate_type':           request.form.get('rate_type', ''),
        'top_waste_materials': request.form.get('top_waste_materials', ''),
        'contamination':       request.form.get('contamination', ''),
        'primary_goal':        request.form.get('primary_goal', ''),
        'open_to_switching':   request.form.get('open_to_switching', ''),
        'program_budget':      request.form.get('program_budget', ''),
        'existing_initiatives':request.form.get('existing_initiatives', ''),
        'additional_notes':    request.form.get('additional_notes', ''),
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
    logger.info(f"[{session_id}] New audit: {form_data['business_name']} | {form_data['business_type']} | {form_data['location']}")
    log_session(session_id, "submission", {
        "business_name": form_data['business_name'],
        "business_type": form_data['business_type'],
        "location": form_data['location'],
        "monthly_spend": form_data['monthly_spend'],
        "waste_hauler": form_data['waste_hauler'],
        "invoice_uploaded": invoice_filename is not None,
        "form_data": form_data
    })

    # Build Claude payload and store in job queue
    user_data_block = build_user_data(form_data)
    message_content = build_message_content(user_data_block, invoice_path, invoice_filename)

    token = str(uuid.uuid4())
    _jobs[token] = {
        "session_id": session_id,
        "business_name": form_data['business_name'],
        "message_content": message_content
    }

    return render_template('results.html',
                           business_name=form_data['business_name'],
                           token=token)


@app.route('/stream/<token>')
def stream(token):
    """SSE endpoint — streams Claude response chunk by chunk."""

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
            logger.info(f"[{session_id}] Starting Claude stream for: {business_name}")

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
        response_len = comp.get("response_length", err.get("error", ""))
        response_html = comp.get("response_html", "")

        rows += f"""
        <tr style="border-bottom:1px solid #eee">
            <td style="padding:.8rem;white-space:nowrap">{timestamp}</td>
            <td style="padding:.8rem"><strong>{business}</strong><br/>
                <small style="color:#888">{btype} · {location}</small></td>
            <td style="padding:.8rem">${spend}/mo<br/><small style="color:#888">{hauler}</small></td>
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
