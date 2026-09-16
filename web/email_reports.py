"""
Email Reports - Send dashboard reports via email.

Supports sending dashboard data as email attachments or inline HTML reports.
Integrates with scheduled exports for automated email delivery.
"""

import os
import json
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Dict, Any, List, Optional
from datetime import datetime

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
EMAIL_CONFIG_FILE = os.path.join(METADATA_DIR, "email_config.json")
EMAIL_HISTORY_FILE = os.path.join(METADATA_DIR, "email_history.json")

# Default email configuration
DEFAULT_EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "use_tls": True,
    "sender_email": "",
    "sender_password": "",
    "sender_name": "Databricks Local Studio",
    "enabled": False
}

# Email configuration
EMAIL_CONFIG = {}

# Email send history
EMAIL_HISTORY = []


def load_email_config():
    """Load email configuration from file."""
    global EMAIL_CONFIG
    if os.path.exists(EMAIL_CONFIG_FILE):
        try:
            with open(EMAIL_CONFIG_FILE, "r") as f:
                EMAIL_CONFIG = json.load(f)
        except Exception as e:
            print(f"Error loading email config: {e}")
            EMAIL_CONFIG = DEFAULT_EMAIL_CONFIG.copy()
    else:
        EMAIL_CONFIG = DEFAULT_EMAIL_CONFIG.copy()
    return EMAIL_CONFIG


def save_email_config():
    """Save email configuration to file."""
    try:
        with open(EMAIL_CONFIG_FILE, "w") as f:
            json.dump(EMAIL_CONFIG, f, indent=2)
    except Exception as e:
        print(f"Error saving email config: {e}")


def load_email_history():
    """Load email send history from file."""
    global EMAIL_HISTORY
    if os.path.exists(EMAIL_HISTORY_FILE):
        try:
            with open(EMAIL_HISTORY_FILE, "r") as f:
                EMAIL_HISTORY = json.load(f)
        except Exception:
            EMAIL_HISTORY = []
    return EMAIL_HISTORY


def save_email_history():
    """Save email send history to file."""
    try:
        # Keep only last 100 entries
        history_to_save = EMAIL_HISTORY[-100:] if len(EMAIL_HISTORY) > 100 else EMAIL_HISTORY
        with open(EMAIL_HISTORY_FILE, "w") as f:
            json.dump(history_to_save, f, indent=2)
    except Exception as e:
        print(f"Error saving email history: {e}")


def update_email_config(
    smtp_server: Optional[str] = None,
    smtp_port: Optional[int] = None,
    use_tls: Optional[bool] = None,
    sender_email: Optional[str] = None,
    sender_password: Optional[str] = None,
    sender_name: Optional[str] = None,
    enabled: Optional[bool] = None
) -> bool:
    """Update email configuration."""
    load_email_config()

    if smtp_server is not None:
        EMAIL_CONFIG["smtp_server"] = smtp_server
    if smtp_port is not None:
        EMAIL_CONFIG["smtp_port"] = smtp_port
    if use_tls is not None:
        EMAIL_CONFIG["use_tls"] = use_tls
    if sender_email is not None:
        EMAIL_CONFIG["sender_email"] = sender_email
    if sender_password is not None:
        EMAIL_CONFIG["sender_password"] = sender_password
    if sender_name is not None:
        EMAIL_CONFIG["sender_name"] = sender_name
    if enabled is not None:
        EMAIL_CONFIG["enabled"] = enabled

    save_email_config()
    return True


def get_email_config() -> Dict[str, Any]:
    """Get current email configuration (without password)."""
    load_email_config()
    config = EMAIL_CONFIG.copy()
    # Don't expose password
    if "sender_password" in config:
        config["sender_password"] = "***" if config["sender_password"] else ""
    return config


def test_email_connection() -> Dict[str, Any]:
    """Test SMTP connection and authentication."""
    load_email_config()

    if not EMAIL_CONFIG.get("enabled"):
        return {"success": False, "error": "Email is not enabled"}

    if not EMAIL_CONFIG.get("sender_email") or not EMAIL_CONFIG.get("sender_password"):
        return {"success": False, "error": "Email credentials not configured"}

    try:
        server = smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"])

        if EMAIL_CONFIG.get("use_tls", True):
            server.starttls()

        server.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["sender_password"])
        server.quit()

        return {"success": True, "message": "Email connection successful"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def send_email(
    recipients: List[str],
    subject: str,
    body_html: str,
    body_text: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Send an email with optional attachments."""
    load_email_config()

    if not EMAIL_CONFIG.get("enabled"):
        return {"success": False, "error": "Email is not enabled"}

    if not EMAIL_CONFIG.get("sender_email") or not EMAIL_CONFIG.get("sender_password"):
        return {"success": False, "error": "Email credentials not configured"}

    if not recipients:
        return {"success": False, "error": "No recipients specified"}

    try:
        # Create message
        msg = MIMEMultipart('alternative')
        msg['From'] = f"{EMAIL_CONFIG.get('sender_name', 'Databricks Local Studio')} <{EMAIL_CONFIG['sender_email']}>"
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        msg['Date'] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")

        if cc:
            msg['Cc'] = ', '.join(cc)

        # Add text and HTML parts
        if body_text:
            msg.attach(MIMEText(body_text, 'plain'))
        msg.attach(MIMEText(body_html, 'html'))

        # Add attachments
        if attachments:
            for filepath in attachments:
                if os.path.exists(filepath):
                    with open(filepath, 'rb') as f:
                        part = MIMEBase('application', 'octet-stream')
                        part.set_payload(f.read())
                        encoders.encode_base64(part)
                        filename = os.path.basename(filepath)
                        part.add_header('Content-Disposition', f'attachment; filename= {filename}')
                        msg.attach(part)

        # Send email
        server = smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"])

        if EMAIL_CONFIG.get("use_tls", True):
            server.starttls()

        server.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["sender_password"])

        all_recipients = recipients + (cc or []) + (bcc or [])
        server.sendmail(EMAIL_CONFIG["sender_email"], all_recipients, msg.as_string())
        server.quit()

        # Record in history
        load_email_history()
        EMAIL_HISTORY.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "recipients": recipients,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "status": "sent",
            "attachments": len(attachments) if attachments else 0
        })
        save_email_history()

        return {"success": True, "message": f"Email sent to {len(all_recipients)} recipient(s)"}

    except Exception as e:
        # Record failure in history
        load_email_history()
        EMAIL_HISTORY.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "recipients": recipients,
            "subject": subject,
            "status": "failed",
            "error": str(e)
        })
        save_email_history()

        return {"success": False, "error": str(e)}


def send_dashboard_report(
    dashboard_id: str,
    dashboard_name: str,
    recipients: List[str],
    include_attachments: bool = True,
    attachment_format: str = "csv",
    export_dir: Optional[str] = None,
    custom_message: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Send a dashboard report email."""

    subject = f"Dashboard Report: {dashboard_name}"

    # Build HTML email body
    body_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                color: #334155;
                line-height: 1.6;
                margin: 0;
                padding: 0;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background: linear-gradient(135deg, #FF3621 0%, #FF6B4A 100%);
                color: white;
                padding: 30px 20px;
                border-radius: 8px 8px 0 0;
                text-align: center;
            }}
            .header h1 {{
                margin: 0;
                font-size: 24px;
                font-weight: 600;
            }}
            .content {{
                background: white;
                padding: 30px 20px;
                border: 1px solid #e2e8f0;
                border-top: none;
            }}
            .footer {{
                background: #f8fafc;
                padding: 20px;
                border-radius: 0 0 8px 8px;
                text-align: center;
                font-size: 12px;
                color: #64748b;
                border: 1px solid #e2e8f0;
                border-top: none;
            }}
            .message {{
                background: #f1f5f9;
                padding: 15px;
                border-radius: 6px;
                margin: 15px 0;
                border-left: 4px solid #6366f1;
            }}
            .info-box {{
                background: #fef3c7;
                padding: 15px;
                border-radius: 6px;
                margin: 15px 0;
                border-left: 4px solid #f59e0b;
            }}
            .timestamp {{
                color: #64748b;
                font-size: 14px;
                margin: 10px 0;
            }}
            a {{
                color: #6366f1;
                text-decoration: none;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📊 Dashboard Report</h1>
            </div>
            <div class="content">
                <h2 style="margin-top: 0; color: #1e293b;">{dashboard_name}</h2>
                <p class="timestamp">Generated: {datetime.now().strftime("%B %d, %Y at %I:%M %p")}</p>

                {f'<div class="message">{custom_message}</div>' if custom_message else ''}

                <p>Your dashboard report has been generated and is ready for review.</p>

                {f'''<div class="info-box">
                    <strong>📎 Attachments Included:</strong><br>
                    Dashboard data exported in {attachment_format.upper()} format
                </div>''' if include_attachments else ''}

                <p style="margin-top: 25px;">
                    <strong>What's Next?</strong><br>
                    • Review the attached data files<br>
                    • Share insights with your team<br>
                    • Schedule regular reports for automated delivery
                </p>
            </div>
            <div class="footer">
                Powered by <strong>Databricks Local Studio</strong><br>
                Automated Dashboard Reporting System
            </div>
        </div>
    </body>
    </html>
    """

    body_text = f"""
Dashboard Report: {dashboard_name}

Generated: {datetime.now().strftime("%B %d, %Y at %I:%M %p")}

{custom_message if custom_message else ''}

Your dashboard report has been generated and is ready for review.

{'Attachments: Dashboard data exported in ' + attachment_format.upper() + ' format' if include_attachments else ''}

---
Powered by Databricks Local Studio
Automated Dashboard Reporting System
    """

    attachments = []
    if include_attachments and export_dir and os.path.exists(export_dir):
        # Collect all files from export directory
        for filename in os.listdir(export_dir):
            filepath = os.path.join(export_dir, filename)
            if os.path.isfile(filepath):
                attachments.append(filepath)

    return send_email(
        recipients=recipients,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        attachments=attachments if attachments else None,
        cc=cc,
        bcc=bcc
    )


def get_email_history(limit: int = 50) -> List[Dict[str, Any]]:
    """Get email send history."""
    load_email_history()
    return EMAIL_HISTORY[-limit:] if len(EMAIL_HISTORY) > limit else EMAIL_HISTORY
