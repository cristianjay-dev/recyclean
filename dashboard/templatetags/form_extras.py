# dashboard/templatetags/form_extras.py
from django import template

register = template.Library()

@register.filter(name="add_class")
def add_class(field, css):
    """
    Append CSS classes to a bound field’s widget while preserving any existing classes.
    Usage: {{ field|add_class:"w-full px-3 py-2" }}
    """
    if not hasattr(field, "as_widget"):
        return field
    attrs = field.field.widget.attrs.copy()
    existing = attrs.get("class", "").strip()
    extra = (css or "").strip()
    if existing and extra:
        new = f"{existing} {extra}"
    else:
        new = existing or extra
    attrs["class"] = new
    return field.as_widget(attrs=attrs)
