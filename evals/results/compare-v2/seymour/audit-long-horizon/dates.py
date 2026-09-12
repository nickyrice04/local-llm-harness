"""Date helpers. All dates are ISO strings (YYYY-MM-DD) at the boundaries."""

from datetime import date, datetime, timedelta

from lib.config import DATE_FORMAT


def parse(text):
    return datetime.strptime(text, DATE_FORMAT).date()


def fmt(day):
    return day.strftime(DATE_FORMAT)


def add_days(text, days):
    # Inclusive of the starting day.
    return fmt(parse(text) + timedelta(days=days))


def days_between(earlier, later):
    """Whole days from `earlier` to `later` (positive when later is after earlier)."""
    return (parse(later) - parse(earlier)).days


def today():
    return fmt(date.today())
