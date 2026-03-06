import os
import base64
from flask import Flask, request, render_template
import anthropic
from pypdf import PdfReader
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Use /tmp for uploads — works on Railway and any ephemeral filesystem
UPLOAD_FOLDER = '/tmp/scrapper_uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY automatically


def extract_text_from_pdf(filepath):
    """Extract text from a PDF invoice."""
    try:
        reader = PdfReader(filepath)
        text = ""
        for page in reader.pages:
            text += page.extract_text()
        return text.strip()
    except Exception as e:
        return f"[Could not extract PDF text: {e}]"


def encode_image_to_base64(filepath):
    """Encode an image file to base64 for Claude."""
    with open(filepath, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def run_audit(form_data, invoice_path, invoice_filename):
    """Send all data to Claude Opus and return the audit report."""

    business_context = f"""
BUSINESS INFORMATION:
- Business Name: {form_data.get('business_name', 'Not provided')}
- Business Type: {form_data.get('business_type', 'Not provided')}
- Location: {form_data.get('location', 'Not provided')}
- Number of Employees: {form_data.get('employees', 'Not provided')}

WASTE SERVICE DETAILS:
- Waste Hauler: {form_data.get('waste_hauler', 'Not provided')}
- Contract End/Renewal Date: {form_data.get('contract_end_date', 'Not provided')}
- Approximate Monthly Spend: ${form_data.get('monthly_spend', 'Not provided')}
- Primary Container Size: {form_data.get('container_size', 'Not provided')}
- Pickup Frequency: {form_data.get('pickup_frequency', 'Not provided')}
- Waste Streams Produced: {', '.join(form_data.get('waste_streams', [])) or 'Not provided'}
- Existing Recycling Program: {form_data.get('recycling_program', 'Not provided')}
- Existing Organics/Composting Program: {form_data.get('organics_program', 'Not provided')}

ADDITIONAL NOTES FROM BUSINESS OWNER:
{form_data.get('additional_notes', 'None provided')}
"""

    system_prompt = """You are Scrapper, an expert waste management consultant with 20 years of experience auditing commercial waste invoices and operations for small and medium businesses across the United States.

Your audit reports are known for being punchy, specific, and immediately actionable. You do not write essays. You lead with the findings. Every issue gets a dollar amount. Every recommendation names a specific vendor, program, or action — not a generic suggestion.

CRITICAL RULES:
- Never write long paragraphs where a structured finding will do
- Always put dollar amounts on findings and recommendations
- Always name specific local vendors, composters, recyclers, and programs relevant to the business's city/region
- If you don't know the exact rate for a local vendor, give a realistic market range for that region
- Quantify GHG/environmental impact in plain terms (e.g. "equivalent to removing X cars from the road")
- Be direct. If something is wrong, say it plainly. Don't soften findings.

Format your response in clean HTML rendered directly in a web page. Use EXACTLY this structure:

<div class="audit-report">

  <div class="audit-section summary-section">
    <h2>Audit Summary</h2>
    <p class="summary-intro">[One punchy sentence: what's the headline finding for this business.]</p>
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
    <h2>🚨 Findings</h2>
    <div class="finding-card red">
      <div class="finding-header">
        <span class="finding-title">[Short title]</span>
        <span class="finding-amount">-$[monthly impact]</span>
      </div>
      <div class="finding-body">
        <strong>What we found:</strong> [Specific to their invoice.]<br/>
        <strong>What it should be:</strong> [The correct amount or industry standard.]<br/>
        <strong>Action:</strong> [Exactly what to do — who to call, what to say.]
      </div>
    </div>
  </div>

  <div class="audit-section">
    <h2>💰 Cost Saving Opportunities</h2>
    <ol class="savings-list">
      <li>
        <strong>[Opportunity title]</strong> — Est. savings: <strong class="green">$[amount]/mo</strong><br/>
        [2-3 sentences max. Be specific: what to change, who to contact, what the new cost would be.]
      </li>
    </ol>
  </div>

  <div class="audit-section">
    <h2>♻️ Diversion & Sustainability</h2>
    <ul class="diversion-list">
      <li>
        <strong>[Stream or opportunity]:</strong> [Specific local composter, MRF, or program. Include estimated cost or savings and GHG impact.]
      </li>
    </ul>
  </div>

  <div class="audit-section">
    <h2>📋 Contract Watch</h2>
    <ul class="contract-list">
      <li><strong>[Contract issue]:</strong> [Specific, actionable advice.]</li>
    </ul>
  </div>

  <div class="audit-section action-plan">
    <h2>✅ Your Action Plan</h2>
    <p class="action-intro">Ranked by impact. Do these in order.</p>
    <ol class="action-list">
      <li>
        <strong>[Action title]</strong> <span class="action-impact">— saves ~$[amount]/mo</span><br/>
        [One sentence: exactly what to do, who to contact, when to do it.]
      </li>
    </ol>
  </div>

</div>

HTML rules:
- Use the exact class names above — they control styling
- finding-card severity: red (billing errors/overcharges), amber (inefficiencies), green (missed opportunities)
- Use <strong> for dollar amounts, key terms, and vendor names
- Use <span class="green"> for savings, <span class="red"> for overcharges
- No inline styles. No CSS blocks. Just the HTML structure above.
- Do not add sections not listed above."""

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
        "text": f"{business_context}\n\nPlease produce a full Scrapper audit report based on the invoice and business information provided above."
    })

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=system_prompt,
        messages=[
            {"role": "user", "content": message_content}
        ]
    )

    return response.content[0].text


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/audit')
def audit_form():
    return render_template('audit_form.html')


@app.route('/submit', methods=['POST'])
def submit():
    form_data = {
        'business_name':     request.form.get('business_name'),
        'business_type':     request.form.get('business_type'),
        'location':          request.form.get('location'),
        'employees':         request.form.get('employees'),
        'waste_hauler':      request.form.get('waste_hauler'),
        'contract_end_date': request.form.get('contract_end_date'),
        'monthly_spend':     request.form.get('monthly_spend'),
        'container_size':    request.form.get('container_size'),
        'pickup_frequency':  request.form.get('pickup_frequency'),
        'waste_streams':     request.form.getlist('waste_streams'),
        'recycling_program': request.form.get('recycling_program'),
        'organics_program':  request.form.get('organics_program'),
        'additional_notes':  request.form.get('additional_notes'),
    }

    invoice_file = request.files.get('invoice')
    invoice_path = None
    invoice_filename = None

    if invoice_file and invoice_file.filename != '':
        invoice_filename = invoice_file.filename
        invoice_path = os.path.join(UPLOAD_FOLDER, invoice_filename)
        invoice_file.save(invoice_path)

    print(f"\n=== New audit request: {form_data['business_name']} ===")
    print(f"Invoice: {invoice_filename or 'None'}")
    print("Sending to Claude Opus 4.6...")

    try:
        audit_html = run_audit(form_data, invoice_path, invoice_filename)
        print("Audit complete.")
        return render_template('results.html',
                               business_name=form_data['business_name'],
                               audit_html=audit_html)
    except Exception as e:
        print(f"Error: {e}")
        return render_template('error.html', error_message=str(e))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
