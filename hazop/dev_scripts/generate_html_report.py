#!/usr/bin/env python3
import json
import html as html_module

# Load data
with open(r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\hazop\consequence_chains_audit.json", 'r', encoding='utf-8') as f:
    chains = json.load(f)

with open(r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\hazop\audit_report.json", 'r', encoding='utf-8') as f:
    audit = json.load(f)

# Build HTML
html_content = """<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HAZOP Consequence Chain Audit Report</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            background-color: #f5f5f5;
            margin: 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        h1 {
            color: #2c3e50;
            border-bottom: 3px solid #3498db;
            padding-bottom: 10px;
        }
        h2 {
            color: #34495e;
            margin-top: 30px;
            border-left: 4px solid #3498db;
            padding-left: 10px;
        }
        h3 {
            color: #7f8c8d;
        }

        .summary-box {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }
        .stat-card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }
        .stat-card.critical {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        }
        .stat-card.high {
            background: linear-gradient(135deg, #ffa751 0%, #ffe259 100%);
        }
        .stat-card.medium {
            background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
        }
        .stat-number {
            font-size: 2.5em;
            font-weight: bold;
        }
        .stat-label {
            font-size: 0.9em;
            opacity: 0.9;
            margin-top: 5px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }
        th {
            background-color: #34495e;
            color: white;
            padding: 12px;
            text-align: left;
        }
        td {
            padding: 12px;
            border-bottom: 1px solid #ecf0f1;
        }
        tr:hover {
            background-color: #f8f9fa;
        }

        .issue-card {
            background: #fff;
            border-left: 5px solid #e74c3c;
            padding: 15px;
            margin: 15px 0;
            border-radius: 4px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .issue-card.high {
            border-left-color: #e67e22;
        }
        .issue-card.medium {
            border-left-color: #f39c12;
        }

        .issue-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .severity-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: bold;
            color: white;
        }
        .severity-badge.CRITICAL {
            background-color: #e74c3c;
        }
        .severity-badge.HIGH {
            background-color: #e67e22;
        }
        .severity-badge.MEDIUM {
            background-color: #f39c12;
        }

        .issue-details {
            font-size: 0.95em;
            line-height: 1.8;
        }
        .issue-details p {
            margin: 8px 0;
        }
        .issue-label {
            color: #7f8c8d;
            font-weight: bold;
        }
        .chain-visual {
            background: #ecf0f1;
            padding: 15px;
            border-radius: 4px;
            margin: 10px 0;
            font-family: monospace;
            font-size: 0.9em;
        }

        .pattern {
            background: #e8f4f8;
            padding: 15px;
            border-radius: 4px;
            margin: 10px 0;
            border-left: 4px solid #3498db;
        }

        .footer {
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #ecf0f1;
            text-align: center;
            color: #7f8c8d;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>HAZOP Consequence Chain Database Audit Report</h1>
        <p><strong>Datum:</strong> 2026-08-02</p>

        <h2>Executive Summary</h2>
        <div class="summary-box">
            <div class="stat-card">
                <div class="stat-number">""" + str(audit['total_records_audited']) + """</div>
                <div class="stat-label">Total Records Audited</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">""" + str(audit['total_steps_found']) + """</div>
                <div class="stat-label">Consequence Steps Found</div>
            </div>
            <div class="stat-card critical">
                <div class="stat-number">""" + str(audit['summary']['CRITICAL']) + """</div>
                <div class="stat-label">CRITICAL Issues</div>
            </div>
            <div class="stat-card high">
                <div class="stat-number">""" + str(audit['summary']['HIGH']) + """</div>
                <div class="stat-label">HIGH Issues</div>
            </div>
            <div class="stat-card medium">
                <div class="stat-number">""" + str(audit['summary']['MEDIUM']) + """</div>
                <div class="stat-label">MEDIUM Issues</div>
            </div>
        </div>

        <h2>Key Findings</h2>
        <div class="pattern">
            <h3>Pattern 1: Empty Consequence Chains (75% of records)</h3>
            <p>3 out of 4 consequence records have NO chain steps defined (Del 1-5 empty).</p>
            <p><strong>Impact:</strong> These consequences are incomplete and provide no logical flow from cause to effect.</p>
            <p><strong>Recommendation:</strong> Populate all consequence records with at least 3-step chains (Del 1-3 minimum).</p>
        </div>

        <div class="pattern">
            <h3>Pattern 2: Underutilization of Chain Steps</h3>
            <p>Only 1 consequence record (ID 1) uses the step structure. It defines 3 steps (Del 1-3) but lacks Del 4-5.</p>
            <p><strong>Current usage:</strong> 3 steps across 4 records = average 0.75 steps/record</p>
            <p><strong>Recommendation:</strong> Consistently define 5-step chains to capture full consequence progression.</p>
        </div>

        <h2>Issues by Severity</h2>

        <h3>CRITICAL Issues (3 total)</h3>
        <p>These represent incomplete consequence chains that lack essential logical flow:</p>
"""

for issue in audit['issues_by_severity']['CRITICAL']:
    cons_id = issue['consequence_id']
    cause = html_module.escape(issue['cause_text'])
    html_content += f"""
        <div class="issue-card">
            <div class="issue-header">
                <span><strong>Consequence ID {cons_id}</strong></span>
                <span class="severity-badge CRITICAL">CRITICAL</span>
            </div>
            <div class="issue-details">
                <p><span class="issue-label">Cause:</span> {cause}</p>
                <p><span class="issue-label">Initial Consequence:</span> {html_module.escape(issue.get('consequence_initial', 'N/A'))}</p>
                <p><span class="issue-label">Issue:</span> {issue['issue']}</p>
                <p><span class="issue-label">Steps Defined:</span> {len(issue.get('steps', {}))} (should be 5)</p>
"""
    if 'missing_steps' in issue:
        html_content += f"                <p><span class=\"issue-label\">Missing:</span> {', '.join(issue['missing_steps'])}</p>\n"
    html_content += f"""
                <p><span class="issue-label">Recommendation:</span> {issue['suggestion']}</p>
            </div>
        </div>
"""

html_content += """
        <h3>HIGH Issues</h3>
        <p>No HIGH severity issues found in this audit.</p>

        <h3>MEDIUM Issues</h3>
        <p>No MEDIUM severity issues found in this audit.</p>

        <h2>All Consequence Records - Detailed View</h2>
        <table>
            <tr>
                <th>Consequence ID</th>
                <th>Cause</th>
                <th>Steps Defined</th>
                <th>Chain Status</th>
            </tr>
"""

for chain in chains:
    step_count = len(chain['steps'])
    status = 'COMPLETE' if step_count >= 5 else 'INCOMPLETE' if step_count > 0 else 'EMPTY'
    cause = html_module.escape(chain['cause_text'][:60] + ('...' if len(chain['cause_text']) > 60 else ''))

    badge_class = ' CRITICAL' if status == 'EMPTY' else ' MEDIUM' if status == 'INCOMPLETE' else ''

    html_content += f"""
            <tr>
                <td>{chain['consequence_id']}</td>
                <td>{cause}</td>
                <td>{step_count}/5</td>
                <td>
                    <span class="severity-badge{badge_class}">{status}</span>
                </td>
            </tr>
"""

html_content += """
        </table>

        <h2>Complete Chain Details</h2>
"""

for chain in chains:
    cons_id = chain['consequence_id']
    steps = chain['steps']
    html_content += f"""
        <div class="issue-card" style="border-left-color: #3498db;">
            <h3>Consequence ID {cons_id}</h3>
            <p><strong>Cause:</strong> {html_module.escape(chain['cause_text'])}</p>
            <p><strong>Initial Description:</strong> {html_module.escape(chain['consequence_initial'])}</p>
            <p><strong>Severity Level:</strong> {chain['severity']}</p>
            <p><strong>Steps:</strong> {len(steps)}/5</p>
"""

    if steps:
        html_content += '            <div class="chain-visual">\n'
        for step_label in sorted(steps.keys(), key=lambda x: int(x.split()[1])):
            text = html_module.escape(steps[step_label].get('text', ''))
            html_content += f'                {step_label}: {text}\n'
        html_content += '            </div>\n'
    else:
        html_content += '            <p><em>No steps defined</em></p>\n'

    html_content += '        </div>\n'

html_content += """
        <h2>Recommendations & Next Steps</h2>
        <ol>
            <li><strong>Immediate (Critical):</strong> Add Del 1-3 steps to Consequence IDs 2, 3, and 4. These records currently have only a brief description with no logical chain.
            <li><strong>Short-term (High):</strong> Extend existing chains to include Del 4-5. Currently only Consequence ID 1 has any steps (3/5), leaving no final consequences.
            <li><strong>Ongoing:</strong> Establish a data entry standard requiring all consequences to have at least a 3-step chain before being marked complete.
            <li><strong>Quality Check:</strong> Review the initial consequence descriptions to ensure they clearly describe the first step (Del 1) of the chain.
        </ol>

        <h2>Technical Notes</h2>
        <ul>
            <li>Database: <code>hazop_project.db</code></li>
            <li>Total consequence records in database: 4</li>
            <li>Total consequence_steps records: 3</li>
            <li>Expected maximum per record: 5 (Del 1, Del 2, Del 3, Del 4, Del 5)</li>
            <li>Audit performed using SQL queries on <code>consequence_steps</code> table</li>
        </ul>

        <div class="footer">
            <p>Generated: 2026-08-02</p>
            <p>Report Type: HAZOP Consequence Chain Audit</p>
            <p>Database: hazop_project.db</p>
        </div>
    </div>
</body>
</html>
"""

# Write HTML report
html_path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\hazop\audit_report.html"
with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html_content)

print(f"HTML report generated: {html_path}")
