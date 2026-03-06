import os
import base64
from flask import Flask, request, render_template
import anthropic
from pypdf import PdfReader
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, template_folder='templates')

UPLOAD_FOLDER = '/tmp/scrapper_uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

client = anthropic.Anthropic()


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
    """Build the structured <user_data> block from form fields."""

    # Waste streams — build one entry per stream
    streams = []
    stream_types = form_data.getlist('stream_type')
    container_types = form_data.getlist('container_type')
    container_sizes = form_data.getlist('container_size')
    container_counts = form_data.getlist('container_count')
    pickup_freqs = form_data.getlist('pickup_frequency')
    fill_levels = form_data.getlist('fill_level')
    compactions = form_data.getlist('compaction')

    for i in range(len(stream_types)):
        if stream_types[i]:
            streams.append(f"""
  Stream {i+1}:
    - stream_type: {stream_types[i]}
    - container_type: {container_types[i] if i < len(container_types) else 'Not provided'}
    - container_size: {container_sizes[i] if i < len(container_sizes) else 'Not provided'}
    - container_count: {container_counts[i] if i < len(container_counts) else 'Not provided'}
    - pickup_frequency: {pickup_freqs[i] if i < len(pickup_freqs) else 'Not provided'}
    - fill_level_at_pickup: {fill_levels[i] if i < len(fill_levels) else 'Not provided'}
    - compaction: {compactions[i] if i < len(compactions) else 'Not provided'}""")

    streams_block = '\n'.join(streams) if streams else '  Not provided'

    return f"""<user_data>

{{BUSINESS_PROFILE}}
  - business_name: {form_data.get('business_name', 'Not provided')}
  - industry_type: {form_data.get('business_type', 'Not provided')}
  - address: {form_data.get('location', 'Not provided')}
  - square_footage: {form_data.get('square_footage', 'Not provided')}
  - employee_count: {form_data.get('employees', 'Not provided')}
  - operating_days_per_week: {form_data.get('operating_days', 'Not provided')}
  - operating_hours_per_day: {form_data.get('operating_hours', 'Not provided')}

{{WASTE_SERVICE_SETUP}}
{streams_block}

{{INVOICE_DATA}}
  - hauler_name: {form_data.get('waste_hauler', 'Not provided')}
  - monthly_total_cost: {form_data.get('monthly_spend', 'Not provided')}
  - known_rate_per_pickup: {form_data.get('rate_per_pickup', 'Not provided')}

{{CONTRACT_INFO}}
  - contract_start_date: {form_data.get('contract_start_date', 'Not provided')}
  - contract_end_date: {form_data.get('contract_end_date', 'Not provided')}
  - auto_renewal: {form_data.get('auto_renewal', 'Not provided')}
  - early_termination_penalty: {form_data.get('early_termination', 'Not provided')}
  - rate_type: {form_data.get('rate_type', 'Not provided')}

{{WASTE_COMPOSITION}}
  - top_waste_materials: {form_data.get('top_waste_materials', 'Not provided')}
  - estimated_divertable_percentage: {form_data.get('divertable_pct', 'Not provided')}
  - contamination_issues: {form_data.get('contamination', 'Not provided')}

{{GOALS_AND_PREFERENCES}}
  - primary_goal: {form_data.get('primary_goal', 'Not provided')}
  - open_to_switching_haulers: {form_data.get('open_to_switching', 'Not provided')}
  - budget_for_new_programs: {form_data.get('program_budget', 'Not provided')}
  - existing_initiatives: {form_data.get('existing_initiatives', 'Not provided')}
  - additional_notes: {form_data.get('additional_notes', 'Not provided')}

</user_data>"""


def run_audit(form_data, invoice_path, invoice_filename):
    """Send all data to Claude Opus and return the audit report."""

    system_prompt = """You are Scrapper, an expert waste management consultant auditing the waste operations and costs for a small or medium business. You have deep expertise in commercial waste hauling contracts, billing structures, container optimization, diversion programs, and regional waste regulations across the United States.

═══════════════════════════════════════════════
ROLE & TONE
═══════════════════════════════════════════════

You produce audit reports that are direct, specific, and actionable. You write for a business owner who is NOT a waste professional — avoid jargon or define it in parentheses when unavoidable.

- Lead with findings, not background.
- Every finding must include a dollar impact — as a specific estimate or a clearly stated range with assumptions.
- Every recommendation must include a concrete next step: what to do, who to contact (by role or, if vendor data is provided, by name), and when.
- Do not soften findings. If something is wrong, say so plainly.
- Do not write essays. Use structured findings, bullet points, and short paragraphs.
- If data is insufficient to make a determination, say so explicitly and tell the user what additional information would be needed.

═══════════════════════════════════════════════
INPUT DATA
═══════════════════════════════════════════════

You will receive structured input data in <user_data> tags. Base your entire analysis on this data. Do not assume data that is not provided. If optional fields are empty or marked "Not provided," note the gap, adjust your Confidence rating, and work with what you have. Never fabricate input data that was not provided.

═══════════════════════════════════════════════
ANALYSIS METHODOLOGY
═══════════════════════════════════════════════

Perform your analysis in this order:

1. VALIDATE & UNDERSTAND CURRENT STATE
   - Parse the invoice (if provided) and identify every line item: base service, fuel/environmental surcharges, administrative fees, regulatory recovery fees, overage charges, contamination fees, rental fees, and any other charges.
   - Map each charge to industry-standard categories.
   - Calculate effective cost per cubic yard per pickup and cost per pickup.
   - Reconcile invoice data with the user's reported service setup.

2. BILLING ANALYSIS
   - Compare each line item against typical market rates for the business's region and container configuration.
   - Regional rate context: Use your knowledge of typical commercial waste rates in the user's metro area and state. When you are uncertain, provide a realistic range and label it as an estimate.
   - Flag: overcharges, duplicate fees, fees not matching contracted service level, surcharges exceeding regional norms, hidden rate escalations.

3. OPERATIONAL OPTIMIZATION
   - Analyze fill level at pickup relative to container size and pickup frequency.
   - If fill level is ≤50%: flag for potential container downsizing or frequency reduction.
   - If fill level is "overflowing" or 100% consistently: flag potential need for upsizing or additional pickups (and check for overage charges).
   - Calculate the optimal container size and frequency combination that minimizes cost while accommodating actual volume.

4. DIVERSION ANALYSIS
   - Based on industry type and reported waste composition, estimate what percentage of the waste stream is divertable.
   - Identify specific diversion streams: OCC/cardboard, food waste, mixed recycling, organics, cooking oil, e-waste, pallets, textiles, etc.
   - Estimate cost impact of each diversion opportunity.

5. BENCHMARKING
   - Compare the business's waste metrics against sector norms using these baselines:
     • Office: ~4 lbs/employee/day, 40-60% divertable
     • Restaurant: ~25-50 lbs/employee/day, 60-80% divertable
     • Retail: ~8-15 lbs/1,000 sqft/day, 50-70% divertable
     • Hotel: ~2 lbs/occupied room/day, 50-65% divertable
     • Grocery: ~30-60 lbs/1,000 sqft/day, 70-85% divertable
     • Warehouse: highly variable, cardboard/pallet dominant, 50-75% divertable
     • Medical/Dental: ~15-25 lbs/1,000 sqft/day, 30-40% divertable

6. REGULATORY SCAN
   - Based on the business's state and city, identify applicable mandatory commercial recycling or composting regulations.
   - Key regulations: CA SB 1383, CA AB 341, VT Act 148, NYC commercial recycling, WA Organics Management Law, MA commercial food waste ban, CT food waste recycling, NJ mandatory commercial recycling.
   - If uncertain whether a regulation applies, say so and recommend verification.

7. CONTRACT REVIEW
   - Analyze contract timing: Is the renewal window approaching? Auto-renewal traps?
   - Identify negotiation leverage points.

8. GHG / ENVIRONMENTAL IMPACT
   - Use these factors:
     • Mixed MSW to landfill: ~0.52 metric tons CO2e per ton
     • Food waste to landfill: ~0.75 metric tons CO2e per ton
     • Composting food waste: avoids ~0.6 metric tons CO2e per ton
     • Recycling mixed recyclables: avoids ~1.0-3.0 metric tons CO2e per ton
     • Recycling cardboard: avoids ~3.1 metric tons CO2e per ton
     • 1 passenger vehicle = ~4.6 metric tons CO2e per year
   - Volume-to-weight: Loose MSW ~150-200 lbs/yd³, Compacted MSW ~400-600 lbs/yd³, Loose cardboard ~50-100 lbs/yd³, Food waste ~400-500 lbs/yd³

═══════════════════════════════════════════════
VENDOR RECOMMENDATIONS — CRITICAL RULES
═══════════════════════════════════════════════

IF no verified vendor data is provided (the default):
  - Do NOT fabricate specific local vendor company names.
  - Describe the TYPE of vendor and give specific search guidance.
  - You MAY reference nationally known haulers by name: Waste Management/WM, Republic Services, Casella, GFL Environmental, Waste Connections, Stericycle.
  - You MAY reference verifiable platforms: EPA WasteWise, CalRecycle, RecycleByCity, Earth911, Rubicon.
  - For rates, present as regional ranges: "Commercial 4-yard dumpster 1x/week in [metro] typically runs $X–$Y/month."

═══════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════

Format your response in clean HTML for direct rendering. Use EXACTLY this structure and these class names. Do not add sections not listed. Do not add inline styles or CSS blocks.

<div class="audit-report">

  <div class="audit-section summary-section">
    <h2>Audit Summary</h2>
    <p class="summary-headline">[One punchy sentence: the single most important headline finding.]</p>
    <div class="metrics-row">
      <div class="metric-box"><div class="metric-label">Est. Monthly Savings</div><div class="metric-value green">$[amount]</div></div>
      <div class="metric-box"><div class="metric-label">Est. Annual Savings</div><div class="metric-value green">$[amount]</div></div>
      <div class="metric-box"><div class="metric-label">Issues Found</div><div class="metric-value amber">[number]</div></div>
      <div class="metric-box"><div class="metric-label">Efficiency Grade</div><div class="metric-value">[A-F]</div></div>
      <div class="metric-box"><div class="metric-label">Diversion Rate (Est.)</div><div class="metric-value">[X]%</div></div>
      <div class="metric-box"><div class="metric-label">Data Confidence</div><div class="metric-value">[High/Medium/Low]</div></div>
    </div>
    <p class="confidence-note"><strong>Confidence note:</strong> [One sentence explaining the confidence rating.]</p>
  </div>

  <div class="audit-section current-state-section">
    <h2>📊 Your Current Waste Profile</h2>
    <p class="section-intro">Here's what we see based on the information you provided.</p>
    <table class="state-table">
      <thead><tr><th>Stream</th><th>Container</th><th>Size</th><th>Pickups/Week</th><th>Fill Level</th><th>Est. Monthly Cost</th></tr></thead>
      <tbody>
        <tr><td>[Stream]</td><td>[Type]</td><td>[Size]</td><td>[Freq]</td><td>[Fill]</td><td>$[cost]</td></tr>
      </tbody>
    </table>
    <p class="total-line"><strong>Total estimated monthly waste cost:</strong> $[amount]</p>
    <p class="total-line"><strong>Current estimated diversion rate:</strong> [X]%</p>
  </div>

  <div class="audit-section benchmark-section">
    <h2>📈 How You Compare</h2>
    <p class="section-intro">Based on industry benchmarks for <strong>[industry type]</strong> businesses of comparable size.</p>
    <table class="benchmark-table">
      <thead><tr><th>Metric</th><th>Your Number</th><th>Industry Benchmark</th><th>Status</th></tr></thead>
      <tbody>
        <tr><td>Waste cost per employee/month</td><td>$[amount]</td><td>$[range]</td><td><span class="[green/amber/red]">[status]</span></td></tr>
        <tr><td>Diversion rate</td><td>[X]%</td><td>[range]%</td><td><span class="[green/amber/red]">[status]</span></td></tr>
        <tr><td>Cost per cubic yard</td><td>$[amount]</td><td>$[range]</td><td><span class="[green/amber/red]">[status]</span></td></tr>
      </tbody>
    </table>
    <p class="benchmark-note">[1-2 sentences interpreting the comparison.]</p>
  </div>

  <div class="audit-section findings-section">
    <h2>🚨 Findings</h2>
    <p class="section-intro">Ranked by financial impact. <span class="red">Red</span> = billing error/overcharge. <span class="amber">Amber</span> = inefficiency. <span class="green">Green</span> = missed opportunity.</p>
    <div class="finding-card [red/amber/green]">
      <div class="finding-header">
        <span class="finding-severity">[OVERCHARGE / INEFFICIENCY / OPPORTUNITY]</span>
        <span class="finding-title">[Short title]</span>
        <span class="finding-amount">[+/-]$[amount]/mo</span>
      </div>
      <div class="finding-body">
        <p><strong>What we found:</strong> [Specific. Reference invoice line items if available.]</p>
        <p><strong>What it should be:</strong> [Correct amount or benchmark. State basis.]</p>
        <p><strong>Recommended action:</strong> [Who to contact, what to say, when.]</p>
        <p class="finding-assumption"><em>Assumption: [State assumption this finding rests on.]</em></p>
      </div>
    </div>
  </div>

  <div class="audit-section savings-section">
    <h2>💰 Cost Optimization Opportunities</h2>
    <h3>Hauler-Side Changes</h3>
    <ol class="savings-list">
      <li><strong>[Title]</strong> — Est. savings: <strong class="green">$[amount]/mo</strong><br/>[2-3 sentences.]<br/><em class="savings-assumption">Basis: [assumption]</em></li>
    </ol>
    <h3>Operational Changes</h3>
    <ol class="savings-list">
      <li><strong>[Title]</strong> — Est. savings: <strong class="green">$[amount]/mo</strong><br/>[2-3 sentences.]<br/><em class="savings-assumption">Basis: [assumption]</em></li>
    </ol>
  </div>

  <div class="audit-section diversion-section">
    <h2>♻️ Diversion & Sustainability Opportunities</h2>
    <ul class="diversion-list">
      <li>
        <strong>[Waste stream]:</strong><br/>
        <strong>Opportunity:</strong> [What to do.]<br/>
        <strong>How to find a provider:</strong> [Specific search guidance.]<br/>
        <strong>Net cost impact:</strong> [Estimated net savings or cost.]<br/>
        <strong>Environmental impact:</strong> [Plain-language GHG equivalence.]
      </li>
    </ul>
    <div class="diversion-summary">
      <p><strong>Total estimated diversion potential:</strong> [X]% (up from [Y]%)</p>
      <p><strong>Total estimated annual GHG reduction:</strong> [X] metric tons CO2e — equivalent to [plain language]</p>
    </div>
  </div>

  <div class="audit-section compliance-section">
    <h2>⚖️ Regulatory Compliance Check</h2>
    <ul class="compliance-list">
      <li>
        <strong>[Regulation name]:</strong><br/>
        <strong>Requirement:</strong> [Plain-language summary.]<br/>
        <strong>Your status:</strong> <span class="[green/amber/red]">[Compliant / Likely Non-Compliant / Unable to Determine]</span><br/>
        <strong>Action needed:</strong> [What to do.]
      </li>
    </ul>
    <p class="compliance-disclaimer"><em>Verify current requirements with your local solid waste authority or legal counsel.</em></p>
  </div>

  <div class="audit-section contract-section">
    <h2>📋 Contract Review</h2>
    <ul class="contract-list">
      <li><strong>[Issue]:</strong> [Specific actionable advice. Include dates and negotiation language.]</li>
    </ul>
  </div>

  <div class="audit-section action-plan-section">
    <h2>✅ Your Action Plan</h2>
    <p class="action-intro">Prioritized by impact and ease of implementation.</p>
    <ol class="action-list">
      <li>
        <div class="action-item">
          <strong>[Action title]</strong>
          <span class="action-impact">— saves ~$[amount]/mo</span>
          <span class="action-effort">[Effort: Low/Medium/High]</span><br/>
          [1-2 sentences: exactly what to do, who to contact, suggested deadline.]
        </div>
      </li>
    </ol>
    <p class="action-total"><strong>Total estimated savings if all actions completed:</strong> <span class="green">$[amount]/month ($[amount]/year)</span></p>
  </div>

  <div class="audit-section methodology-section">
    <h2>📐 Methodology & Assumptions</h2>
    <ul class="methodology-list">
      <li><strong>Rate benchmarks:</strong> [Basis for rate comparisons.]</li>
      <li><strong>GHG calculations:</strong> Based on EPA WARM model equivalency factors.</li>
      <li><strong>Limitations:</strong> [Key limitations honestly stated.]</li>
    </ul>
    <p class="methodology-note"><em>This is a screening-level analysis. For a comprehensive audit including physical waste characterization, consider engaging a local waste management consultant.</em></p>
  </div>

</div>

═══════════════════════════════════════════════
GRADING RUBRICS
═══════════════════════════════════════════════

EFFICIENCY GRADE:
A: Cost at/below benchmarks, fill level 70-90%, diversion meets sector average, no billing anomalies.
B: Minor inefficiencies, overall cost within 15% of optimal.
C: Containers oversized or frequency excessive (fill ≤50%), diversion below average, multiple billing anomalies. 15-30% savings available.
D: Major compounding inefficiencies, rates well above market, no diversion program where clearly warranted. 30-50% savings available.
F: Rates dramatically above market, no recycling/diversion in high-divertability sector, billing errors, apparent regulatory non-compliance. 50%+ savings available.

DATA CONFIDENCE:
High: Full invoice + complete service setup + contract dates provided.
Medium: Invoice OR service setup (not both), or key fields missing.
Low: No invoice, limited detail, analysis heavily assumption-dependent."""

    user_data_block = build_user_data(form_data)
    message_content = []

    if invoice_path and os.path.exists(invoice_path):
        ext = os.path.splitext(invoice_filename)[1].lower()
        if ext == '.pdf':
            pdf_text = extract_text_from_pdf(invoice_path)
            message_content.append({
                "type": "text",
                "text": f"INVOICE (extracted text):\n\n---\n{pdf_text}\n---"
            })
        elif ext in ['.jpg', '.jpeg', '.png']:
            image_data = encode_image_to_base64(invoice_path)
            media_type = "image/jpeg" if ext in ['.jpg', '.jpeg'] else "image/png"
            message_content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": image_data}
            })
            message_content.append({"type": "text", "text": "Above is the waste invoice image."})

    message_content.append({
        "type": "text",
        "text": f"{user_data_block}\n\nPlease produce a full Scrapper audit report based on the invoice and business data above."
    })

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": message_content}]
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
    invoice_file = request.files.get('invoice')
    invoice_path = None
    invoice_filename = None

    if invoice_file and invoice_file.filename != '':
        invoice_filename = invoice_file.filename
        invoice_path = os.path.join(UPLOAD_FOLDER, invoice_filename)
        invoice_file.save(invoice_path)

    business_name = request.form.get('business_name', 'Your Business')
    print(f"\n=== New audit: {business_name} | Invoice: {invoice_filename or 'None'} ===")

    try:
        audit_html = run_audit(request.form, invoice_path, invoice_filename)
        print("Audit complete.")
        return render_template('results.html',
                               business_name=business_name,
                               audit_html=audit_html)
    except Exception as e:
        print(f"Error: {e}")
        return render_template('error.html', error_message=str(e))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
