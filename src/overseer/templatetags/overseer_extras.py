from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def ms(value):
    """Humanise milliseconds: 850 ms, 2.3 s, 4m 05s, 1h 12m."""
    if value is None or value == "":
        return "–"
    value = int(value)
    if value < 1000:
        return f"{value} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@register.filter
def pct(value):
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "–"


@register.filter
def ago(value):
    """Short relative time: 12s ago, 3m ago, 2h ago, 5d ago, or 'in 3m' for future times."""
    if not value:
        return "–"
    delta = timezone.now() - value
    seconds = int(delta.total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = f"{seconds}s"
    elif seconds < 3600:
        text = f"{seconds // 60}m"
    elif seconds < 86400:
        text = f"{seconds // 3600}h"
    else:
        text = f"{seconds // 86400}d"
    return f"in {text}" if future else f"{text} ago"


@register.filter
def short_id(value):
    return str(value)[:8]


@register.filter
def bar_width(value, maximum):
    try:
        maximum = float(maximum)
        return int(float(value) / maximum * 100) if maximum else 0
    except (TypeError, ValueError):
        return 0
