"""Add argument requirements after the caller's unchanged existing guard."""
from .argument_units import source_issues

def guard(book,existing_guard):
    baseline=existing_guard(book)
    failures=source_issues(book)
    if failures:raise ValueError('Argument source/graphics support missing or invalid: '+str(failures))
    return baseline
