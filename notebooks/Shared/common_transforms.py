# Shared Lakehouse Data Transformations
# Can be imported by notebooks or executed in workflows

def format_currency(val):
    return f'${val:,.2f}' if val is not None else '$0.00'

def categorize_salary(salary):
    if salary >= 100000:
        return 'Senior / Lead'
    elif salary >= 70000:
        return 'Mid-Level'
    return 'Associate'
