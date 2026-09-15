"""
Custom Dashboard Themes - User-customizable color schemes and styling.
"""

THEMES = {
    "default_dark": {
        "id": "default_dark",
        "name": "Default Dark",
        "colors": {
            "background": "#0b111e",
            "panel": "#0f1729",
            "border": "#1e293b",
            "text": "#f8fafc",
            "accent": "#6366f1"
        }
    },
    "default_light": {
        "id": "default_light",
        "name": "Default Light",
        "colors": {
            "background": "#ffffff",
            "panel": "#f8fafc",
            "border": "#e2e8f0",
            "text": "#0f172a",
            "accent": "#6366f1"
        }
    },
    "ocean": {
        "id": "ocean",
        "name": "Ocean Blue",
        "colors": {
            "background": "#0a192f",
            "panel": "#112240",
            "border": "#233554",
            "text": "#ccd6f6",
            "accent": "#64ffda"
        }
    },
    "forest": {
        "id": "forest",
        "name": "Forest Green",
        "colors": {
            "background": "#1a1f1a",
            "panel": "#2d3a2d",
            "border": "#3d4f3d",
            "text": "#e8f5e8",
            "accent": "#4ade80"
        }
    },
    "sunset": {
        "id": "sunset",
        "name": "Sunset Orange",
        "colors": {
            "background": "#1f1510",
            "panel": "#2d1f19",
            "border": "#3d2f29",
            "text": "#f5e8e0",
            "accent": "#fb923c"
        }
    }
}

def get_all_themes():
    return list(THEMES.values())

def get_theme(theme_id: str):
    return THEMES.get(theme_id)
