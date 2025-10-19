# dashboard/templatetags/form_extra.py
from django import template

register = template.Library()

@register.filter(name="add_class")
def add_class(field, css):
    """Append CSS classes to a bound field’s widget."""
    attrs = field.field.widget.attrs.copy()
    attrs["class"] = (attrs.get("class", "") + " " + css).strip()
    return field.as_widget(attrs=attrs)
