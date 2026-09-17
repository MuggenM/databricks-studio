"""
Dashboard Templates - Pre-built dashboard configurations for common use cases.

Provides quick-start templates that users can instantiate with one click.
"""

DASHBOARD_TEMPLATES = [
    {
        "id": "template_sales_overview",
        "name": "Sales Performance Dashboard",
        "description": "Track sales metrics, revenue trends, and top performers",
        "category": "Sales & Revenue",
        "icon": "chart-line-up",
        "requires_tables": ["sales_data", "customers"],
        "template": {
            "name": "Sales Performance Dashboard",
            "description": "Comprehensive sales analytics with revenue tracking and customer insights",
            "filters": [
                {
                    "key": "date_range",
                    "label": "Date Range",
                    "type": "daterange",
                    "default": "30d"
                },
                {
                    "key": "region",
                    "label": "Region",
                    "type": "select",
                    "dimension": "region",
                    "table": "sales_data",
                    "default": "ALL"
                }
            ],
            "widgets": [
                {
                    "id": "w_total_revenue",
                    "title": "Total Revenue",
                    "type": "kpi",
                    "query": "SELECT SUM(amount) as value FROM sales_data WHERE (:start_date IS NULL OR sale_date >= :start_date) AND (:end_date IS NULL OR sale_date <= :end_date)",
                    "unit": "$",
                    "width": "col-span-1"
                },
                {
                    "id": "w_revenue_trend",
                    "title": "Revenue Trend",
                    "type": "area",
                    "query": "SELECT sale_date, SUM(amount) as revenue FROM sales_data GROUP BY sale_date ORDER BY sale_date",
                    "x_col": "sale_date",
                    "y_col": "revenue",
                    "unit": "$",
                    "width": "col-span-3"
                }
            ]
        }
    },
    {
        "id": "template_hr_analytics",
        "name": "HR Analytics Dashboard",
        "description": "Employee metrics, headcount, compensation, and hiring trends",
        "category": "Human Resources",
        "icon": "users",
        "requires_tables": ["employees"],
        "template": {
            "name": "HR Analytics Dashboard",
            "description": "Complete HR metrics including workforce composition, compensation, and trends",
            "filters": [
                {
                    "key": "department",
                    "label": "Department",
                    "type": "select",
                    "dimension": "department",
                    "table": "employees",
                    "default": "ALL"
                }
            ],
            "widgets": [
                {
                    "id": "w_headcount",
                    "title": "Total Headcount",
                    "type": "kpi",
                    "query": "SELECT COUNT(*) as value FROM employees WHERE (:department IS NULL OR department = :department)",
                    "unit": "employees",
                    "width": "col-span-1"
                },
                {
                    "id": "w_dept_distribution",
                    "title": "Department Distribution",
                    "type": "pie",
                    "query": "SELECT department, COUNT(*) as count FROM employees GROUP BY department",
                    "x_col": "department",
                    "y_col": "count",
                    "width": "col-span-2"
                }
            ]
        }
    },
    {
        "id": "template_marketing",
        "name": "Marketing Campaign Dashboard",
        "description": "Campaign performance, conversion rates, and ROI tracking",
        "category": "Marketing",
        "icon": "megaphone",
        "requires_tables": ["campaigns", "conversions"],
        "template": {
            "name": "Marketing Campaign Dashboard",
            "description": "Track campaign performance, engagement, and conversion metrics",
            "filters": [
                {
                    "key": "campaign",
                    "label": "Campaign",
                    "type": "select",
                    "dimension": "campaign_name",
                    "table": "campaigns",
                    "default": "ALL"
                }
            ],
            "widgets": [
                {
                    "id": "w_total_impressions",
                    "title": "Total Impressions",
                    "type": "kpi",
                    "query": "SELECT SUM(impressions) as value FROM campaigns",
                    "unit": "",
                    "width": "col-span-1"
                },
                {
                    "id": "w_conversion_rate",
                    "title": "Conversion Rate",
                    "type": "gauge",
                    "query": "SELECT (COUNT(DISTINCT conversion_id) * 100.0 / COUNT(DISTINCT visitor_id)) as value FROM conversions",
                    "unit": "%",
                    "width": "col-span-1"
                }
            ]
        }
    },
    {
        "id": "template_operations",
        "name": "Operations KPI Dashboard",
        "description": "Operational efficiency, SLA compliance, and process metrics",
        "category": "Operations",
        "icon": "gear",
        "requires_tables": ["operations_log"],
        "template": {
            "name": "Operations KPI Dashboard",
            "description": "Monitor operational performance and SLA compliance",
            "filters": [],
            "widgets": [
                {
                    "id": "w_uptime",
                    "title": "System Uptime",
                    "type": "gauge",
                    "query": "SELECT 99.8 as value",
                    "unit": "%",
                    "width": "col-span-1"
                },
                {
                    "id": "w_response_time",
                    "title": "Avg Response Time",
                    "type": "kpi",
                    "query": "SELECT AVG(response_ms) as value FROM operations_log",
                    "unit": "ms",
                    "width": "col-span-1"
                }
            ]
        }
    },
    {
        "id": "template_finance",
        "name": "Financial Overview Dashboard",
        "description": "Revenue, expenses, profit margins, and budget tracking",
        "category": "Finance",
        "icon": "currency-dollar",
        "requires_tables": ["transactions"],
        "template": {
            "name": "Financial Overview Dashboard",
            "description": "Complete financial metrics with P&L tracking",
            "filters": [
                {
                    "key": "quarter",
                    "label": "Quarter",
                    "type": "select",
                    "dimension": "quarter",
                    "table": "transactions",
                    "default": "ALL"
                }
            ],
            "widgets": [
                {
                    "id": "w_net_profit",
                    "title": "Net Profit",
                    "type": "kpi",
                    "query": "SELECT SUM(CASE WHEN type='revenue' THEN amount ELSE -amount END) as value FROM transactions",
                    "unit": "$",
                    "width": "col-span-1",
                    "thresholds": {
                        "enabled": True,
                        "critical_low": 0,
                        "warning_low": 50000,
                        "warning_high": None,
                        "critical_high": None
                    }
                }
            ]
        }
    },
    {
        "id": "template_customer_success",
        "name": "Customer Success Dashboard",
        "description": "Customer health scores, churn risk, and satisfaction metrics",
        "category": "Customer Success",
        "icon": "smiley",
        "requires_tables": ["customers", "support_tickets"],
        "template": {
            "name": "Customer Success Dashboard",
            "description": "Monitor customer health and satisfaction",
            "filters": [],
            "widgets": [
                {
                    "id": "w_nps_score",
                    "title": "NPS Score",
                    "type": "gauge",
                    "query": "SELECT 72 as value",
                    "unit": "",
                    "width": "col-span-1"
                },
                {
                    "id": "w_ticket_volume",
                    "title": "Open Tickets",
                    "type": "kpi",
                    "query": "SELECT COUNT(*) as value FROM support_tickets WHERE status='open'",
                    "unit": "tickets",
                    "width": "col-span-1"
                }
            ]
        }
    }
]


def get_all_templates():
    """Return all dashboard templates."""
    return DASHBOARD_TEMPLATES


def get_template_by_id(template_id: str):
    """Get a specific template by ID."""
    return next((t for t in DASHBOARD_TEMPLATES if t["id"] == template_id), None)


def get_templates_by_category(category: str):
    """Get all templates in a specific category."""
    return [t for t in DASHBOARD_TEMPLATES if t["category"] == category]


def instantiate_template(template_id: str, dashboard_name: str = None):
    """
    Create a new dashboard from a template.

    Args:
        template_id: ID of the template to use
        dashboard_name: Optional custom name for the dashboard

    Returns:
        Dashboard configuration dictionary
    """
    template = get_template_by_id(template_id)
    if not template:
        return None

    import copy
    import uuid
    import datetime

    # Deep copy the template to avoid modifying the original
    dashboard_config = copy.deepcopy(template["template"])

    # Generate unique ID
    dashboard_config["id"] = f"dash_{uuid.uuid4().hex[:12]}"

    # Use custom name if provided
    if dashboard_name:
        dashboard_config["name"] = dashboard_name

    # Add metadata
    dashboard_config["created_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dashboard_config["created_from_template"] = template_id
    dashboard_config["auto_refresh_enabled"] = False
    dashboard_config["auto_refresh_interval"] = 30

    return dashboard_config
