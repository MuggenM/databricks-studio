"""
Slack Integration - Send notifications and alerts to Slack channels.

Supports webhook-based notifications for dashboard alerts, query results,
and system events.
"""

import os
import json
import requests
from typing import Dict, Any, List, Optional
from datetime import datetime

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
SLACK_CONFIG_FILE = os.path.join(METADATA_DIR, "slack_config.json")

# Default configuration
DEFAULT_SLACK_CONFIG = {
    "enabled": False,
    "webhooks": [],  # List of webhook configurations
    "default_webhook": None,
    "mention_users": True,
    "include_charts": True
}


def load_config() -> Dict[str, Any]:
    """Load Slack configuration from file."""
    if os.path.exists(SLACK_CONFIG_FILE):
        try:
            with open(SLACK_CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading Slack config: {e}")
            return DEFAULT_SLACK_CONFIG.copy()
    return DEFAULT_SLACK_CONFIG.copy()


def save_config(config: Dict[str, Any]) -> bool:
    """Save Slack configuration to file."""
    try:
        os.makedirs(METADATA_DIR, exist_ok=True)
        with open(SLACK_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving Slack config: {e}")
        return False


def add_webhook(name: str, webhook_url: str, channel: str = "#general", description: str = "") -> Dict[str, Any]:
    """Add a new Slack webhook."""
    config = load_config()

    # Check if webhook already exists
    for webhook in config.get("webhooks", []):
        if webhook["name"] == name:
            return {
                "success": False,
                "error": f"Webhook with name '{name}' already exists"
            }

    webhook = {
        "id": f"webhook_{len(config.get('webhooks', []))}_{int(datetime.now().timestamp())}",
        "name": name,
        "webhook_url": webhook_url,
        "channel": channel,
        "description": description,
        "created_at": datetime.now().isoformat(),
        "enabled": True
    }

    if "webhooks" not in config:
        config["webhooks"] = []

    config["webhooks"].append(webhook)

    # Set as default if it's the first webhook
    if not config.get("default_webhook"):
        config["default_webhook"] = webhook["id"]

    if save_config(config):
        return {
            "success": True,
            "webhook": webhook
        }
    else:
        return {
            "success": False,
            "error": "Failed to save configuration"
        }


def remove_webhook(webhook_id: str) -> Dict[str, Any]:
    """Remove a Slack webhook."""
    config = load_config()

    webhooks = config.get("webhooks", [])
    original_count = len(webhooks)

    config["webhooks"] = [w for w in webhooks if w["id"] != webhook_id]

    if len(config["webhooks"]) < original_count:
        # If removed webhook was default, set new default
        if config.get("default_webhook") == webhook_id:
            config["default_webhook"] = config["webhooks"][0]["id"] if config["webhooks"] else None

        if save_config(config):
            return {"success": True}
        else:
            return {"success": False, "error": "Failed to save configuration"}

    return {"success": False, "error": "Webhook not found"}


def test_webhook(webhook_url: str) -> Dict[str, Any]:
    """Test a Slack webhook connection."""
    try:
        payload = {
            "text": "✅ Test notification from Databricks Local Studio",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Test Notification*\n\nYour Slack integration is working correctly! 🎉"
                    }
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Sent at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        }
                    ]
                }
            ]
        }

        response = requests.post(webhook_url, json=payload, timeout=10)

        if response.status_code == 200:
            return {
                "success": True,
                "message": "Test notification sent successfully"
            }
        else:
            return {
                "success": False,
                "error": f"Slack API returned status {response.status_code}: {response.text}"
            }

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": "Request timed out. Check your webhook URL and network connection."
        }
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "error": f"Connection error: {str(e)}"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


def send_notification(
    message: str,
    webhook_id: Optional[str] = None,
    title: Optional[str] = None,
    fields: Optional[List[Dict[str, str]]] = None,
    color: str = "#36a64f",
    footer: Optional[str] = None
) -> Dict[str, Any]:
    """Send a notification to Slack."""
    config = load_config()

    if not config.get("enabled", False):
        return {
            "success": False,
            "error": "Slack integration is not enabled"
        }

    # Find webhook to use
    webhook = None
    if webhook_id:
        webhook = next((w for w in config.get("webhooks", []) if w["id"] == webhook_id), None)
    else:
        # Use default webhook
        default_id = config.get("default_webhook")
        if default_id:
            webhook = next((w for w in config.get("webhooks", []) if w["id"] == default_id), None)

    if not webhook:
        return {
            "success": False,
            "error": "No webhook configured"
        }

    if not webhook.get("enabled", True):
        return {
            "success": False,
            "error": f"Webhook '{webhook['name']}' is disabled"
        }

    try:
        # Build Slack message payload
        blocks = []

        if title:
            blocks.append({
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": title
                }
            })

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": message
            }
        })

        if fields:
            blocks.append({
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*{field['title']}*\n{field['value']}"
                    }
                    for field in fields
                ]
            })

        if footer:
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": footer
                    }
                ]
            })

        payload = {
            "blocks": blocks,
            "attachments": [{
                "color": color
            }]
        }

        response = requests.post(webhook["webhook_url"], json=payload, timeout=10)

        if response.status_code == 200:
            return {
                "success": True,
                "message": "Notification sent successfully",
                "webhook_name": webhook["name"],
                "channel": webhook.get("channel", "#general")
            }
        else:
            return {
                "success": False,
                "error": f"Slack API returned status {response.status_code}"
            }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def send_alert(
    alert_name: str,
    condition: str,
    current_value: Any,
    threshold: Any,
    dashboard_name: Optional[str] = None,
    query_name: Optional[str] = None,
    severity: str = "warning",
    webhook_id: Optional[str] = None
) -> Dict[str, Any]:
    """Send an alert notification to Slack."""

    severity_colors = {
        "critical": "#dc2626",  # red
        "warning": "#f59e0b",   # amber
        "info": "#3b82f6"       # blue
    }

    severity_icons = {
        "critical": "🔴",
        "warning": "⚠️",
        "info": "ℹ️"
    }

    color = severity_colors.get(severity, "#f59e0b")
    icon = severity_icons.get(severity, "⚠️")

    title = f"{icon} Alert: {alert_name}"

    message = f"*Condition:* {condition}\n*Current Value:* `{current_value}`\n*Threshold:* `{threshold}`"

    fields = []

    if dashboard_name:
        fields.append({
            "title": "Dashboard",
            "value": dashboard_name
        })

    if query_name:
        fields.append({
            "title": "Query",
            "value": query_name
        })

    fields.append({
        "title": "Severity",
        "value": severity.upper()
    })

    fields.append({
        "title": "Time",
        "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

    footer = "📊 Databricks Local Studio Alert"

    return send_notification(
        message=message,
        webhook_id=webhook_id,
        title=title,
        fields=fields,
        color=color,
        footer=footer
    )


def send_query_result(
    query_name: str,
    row_count: int,
    execution_time: float,
    dashboard_name: Optional[str] = None,
    webhook_id: Optional[str] = None,
    sample_data: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """Send query execution result to Slack."""

    title = f"✅ Query Completed: {query_name}"

    message = f"Query executed successfully"

    fields = [
        {
            "title": "Row Count",
            "value": f"{row_count:,} rows"
        },
        {
            "title": "Execution Time",
            "value": f"{execution_time:.2f}s"
        }
    ]

    if dashboard_name:
        fields.append({
            "title": "Dashboard",
            "value": dashboard_name
        })

    fields.append({
        "title": "Time",
        "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

    footer = "📊 Databricks Local Studio"

    return send_notification(
        message=message,
        webhook_id=webhook_id,
        title=title,
        fields=fields,
        color="#10b981",  # green
        footer=footer
    )


def get_webhooks() -> List[Dict[str, Any]]:
    """Get all configured webhooks."""
    config = load_config()
    return config.get("webhooks", [])


def get_webhook(webhook_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific webhook by ID."""
    webhooks = get_webhooks()
    return next((w for w in webhooks if w["id"] == webhook_id), None)


def update_webhook(webhook_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update a webhook configuration."""
    config = load_config()

    webhooks = config.get("webhooks", [])
    webhook = next((w for w in webhooks if w["id"] == webhook_id), None)

    if not webhook:
        return {
            "success": False,
            "error": "Webhook not found"
        }

    # Update allowed fields
    allowed_fields = ["name", "webhook_url", "channel", "description", "enabled"]
    for field in allowed_fields:
        if field in updates:
            webhook[field] = updates[field]

    webhook["updated_at"] = datetime.now().isoformat()

    if save_config(config):
        return {
            "success": True,
            "webhook": webhook
        }
    else:
        return {
            "success": False,
            "error": "Failed to save configuration"
        }
