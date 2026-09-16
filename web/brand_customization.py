"""
Brand Customization - Customize the application's appearance and branding.

Supports custom logos, color schemes, company name, and visual theming.
"""

import os
import json
import base64
from typing import Dict, Any, Optional
from datetime import datetime

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
BRAND_CONFIG_FILE = os.path.join(METADATA_DIR, "brand_config.json")
BRAND_ASSETS_DIR = os.path.join(METADATA_DIR, "brand_assets")

# Default brand configuration
DEFAULT_BRAND_CONFIG = {
    "company_name": "Databricks Local Studio",
    "app_title": "Databricks Local Studio",
    "logo_url": None,
    "logo_dark_url": None,
    "favicon_url": None,
    "primary_color": "#6366f1",      # Indigo
    "secondary_color": "#8b5cf6",    # Purple
    "accent_color": "#3b82f6",       # Blue
    "success_color": "#10b981",      # Green
    "warning_color": "#f59e0b",      # Amber
    "error_color": "#ef4444",        # Red
    "sidebar_bg": "#0b111e",
    "panel_bg": "#141b2d",
    "border_color": "#1e293b",
    "footer_text": "Powered by Databricks Local Studio",
    "show_footer": True,
    "custom_css": "",
    "login_message": "Welcome back! Sign in to continue.",
    "enable_custom_theme": False
}


def ensure_directories():
    """Ensure brand assets directory exists."""
    os.makedirs(METADATA_DIR, exist_ok=True)
    os.makedirs(BRAND_ASSETS_DIR, exist_ok=True)


def load_config() -> Dict[str, Any]:
    """Load brand configuration from file."""
    ensure_directories()

    if os.path.exists(BRAND_CONFIG_FILE):
        try:
            with open(BRAND_CONFIG_FILE, "r") as f:
                config = json.load(f)
                # Merge with defaults to ensure all keys exist
                return {**DEFAULT_BRAND_CONFIG, **config}
        except Exception as e:
            print(f"Error loading brand config: {e}")
            return DEFAULT_BRAND_CONFIG.copy()
    return DEFAULT_BRAND_CONFIG.copy()


def save_config(config: Dict[str, Any]) -> bool:
    """Save brand configuration to file."""
    try:
        ensure_directories()
        with open(BRAND_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving brand config: {e}")
        return False


def save_logo(image_data: str, logo_type: str = "light") -> Dict[str, Any]:
    """
    Save logo image from base64 data.

    Args:
        image_data: Base64 encoded image data (with or without data URL prefix)
        logo_type: "light" (for dark backgrounds) or "dark" (for light backgrounds)

    Returns:
        Dict with success status and logo URL
    """
    try:
        ensure_directories()

        # Remove data URL prefix if present
        if ',' in image_data and image_data.startswith('data:'):
            image_data = image_data.split(',', 1)[1]

        # Decode base64
        image_bytes = base64.b64decode(image_data)

        # Determine file extension from data
        file_ext = "png"  # Default
        if image_data.startswith("iVBOR"):
            file_ext = "png"
        elif image_data.startswith("/9j/"):
            file_ext = "jpg"
        elif image_data.startswith("R0lG"):
            file_ext = "gif"
        elif image_data.startswith("PHN2Zy") or image_data.startswith("PD94bW"):
            file_ext = "svg"

        # Generate filename
        timestamp = int(datetime.now().timestamp())
        filename = f"logo_{logo_type}_{timestamp}.{file_ext}"
        filepath = os.path.join(BRAND_ASSETS_DIR, filename)

        # Save file
        with open(filepath, 'wb') as f:
            f.write(image_bytes)

        # Generate URL path
        logo_url = f"/api/brand/assets/{filename}"

        # Update config
        config = load_config()
        if logo_type == "light":
            config["logo_url"] = logo_url
        elif logo_type == "dark":
            config["logo_dark_url"] = logo_url
        elif logo_type == "favicon":
            config["favicon_url"] = logo_url

        save_config(config)

        return {
            "success": True,
            "logo_url": logo_url,
            "filename": filename
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def delete_logo(logo_type: str) -> Dict[str, Any]:
    """Delete a logo and reset to default."""
    try:
        config = load_config()

        # Get current logo URL
        logo_url = None
        if logo_type == "light":
            logo_url = config.get("logo_url")
            config["logo_url"] = None
        elif logo_type == "dark":
            logo_url = config.get("logo_dark_url")
            config["logo_dark_url"] = None
        elif logo_type == "favicon":
            logo_url = config.get("favicon_url")
            config["favicon_url"] = None

        # Delete file if it exists
        if logo_url:
            filename = logo_url.split('/')[-1]
            filepath = os.path.join(BRAND_ASSETS_DIR, filename)
            if os.path.exists(filepath):
                os.remove(filepath)

        save_config(config)

        return {
            "success": True,
            "message": f"{logo_type} logo deleted"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def get_asset_path(filename: str) -> Optional[str]:
    """Get full path to a brand asset file."""
    filepath = os.path.join(BRAND_ASSETS_DIR, filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        return filepath
    return None


def update_theme_colors(colors: Dict[str, str]) -> Dict[str, Any]:
    """Update theme color scheme."""
    config = load_config()

    allowed_colors = [
        "primary_color", "secondary_color", "accent_color",
        "success_color", "warning_color", "error_color",
        "sidebar_bg", "panel_bg", "border_color"
    ]

    for key, value in colors.items():
        if key in allowed_colors and isinstance(value, str):
            # Validate hex color format
            if value.startswith('#') and len(value) in [4, 7]:
                config[key] = value

    if save_config(config):
        return {"success": True, "config": config}
    else:
        return {"success": False, "error": "Failed to save configuration"}


def reset_to_defaults() -> Dict[str, Any]:
    """Reset brand customization to default values."""
    try:
        # Keep existing logos but reset other settings
        current_config = load_config()
        default_config = DEFAULT_BRAND_CONFIG.copy()

        # Preserve logos
        default_config["logo_url"] = current_config.get("logo_url")
        default_config["logo_dark_url"] = current_config.get("logo_dark_url")
        default_config["favicon_url"] = current_config.get("favicon_url")

        if save_config(default_config):
            return {"success": True, "config": default_config}
        else:
            return {"success": False, "error": "Failed to reset configuration"}

    except Exception as e:
        return {"success": False, "error": str(e)}


def get_css_variables() -> str:
    """Generate CSS custom properties from brand config."""
    config = load_config()

    if not config.get("enable_custom_theme", False):
        return ""

    css = ":root {\n"
    css += f"  --brand-primary: {config.get('primary_color', '#6366f1')};\n"
    css += f"  --brand-secondary: {config.get('secondary_color', '#8b5cf6')};\n"
    css += f"  --brand-accent: {config.get('accent_color', '#3b82f6')};\n"
    css += f"  --brand-success: {config.get('success_color', '#10b981')};\n"
    css += f"  --brand-warning: {config.get('warning_color', '#f59e0b')};\n"
    css += f"  --brand-error: {config.get('error_color', '#ef4444')};\n"
    css += f"  --brand-sidebar-bg: {config.get('sidebar_bg', '#0b111e')};\n"
    css += f"  --brand-panel-bg: {config.get('panel_bg', '#141b2d')};\n"
    css += f"  --brand-border: {config.get('border_color', '#1e293b')};\n"
    css += "}\n"

    return css


def validate_color(color: str) -> bool:
    """Validate hex color format."""
    if not isinstance(color, str):
        return False

    if not color.startswith('#'):
        return False

    if len(color) not in [4, 7]:
        return False

    try:
        int(color[1:], 16)
        return True
    except ValueError:
        return False
