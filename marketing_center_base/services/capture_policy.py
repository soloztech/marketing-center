"""Record when a company's marketing capture policy last changed.

The dashboard states capture policy, not measurement completeness: only the
instant of an effective change (or of the upgrade that introduced tracking)
is known. Nothing is inferred from recorded events.
"""

from odoo import fields

CAPTURE_STAMP_TOKEN = object()
_CONTEXT_KEY = "marketing_capture_stamp_token"


def _may_stamp(records):
    return records.env.context.get(_CONTEXT_KEY) is CAPTURE_STAMP_TOKEN


def strip_manual_stamp(records, values, stamp_field):
    """Drop a caller-supplied stamp; only the policy change may set it."""

    if stamp_field in values and not _may_stamp(records):
        values = dict(values)
        values.pop(stamp_field)
    return values


def neutralize_create_stamp(records, values, stamp_field):
    """Create without any external stamp, including context or ir.default ones.

    An explicit ``False`` prevents ORM defaults from filling the field; enabled
    companies are stamped internally right after creation.
    """

    if _may_stamp(records):
        return values
    return dict(values, **{stamp_field: False})


def stamp(records, stamp_field):
    if records:
        records.with_context(**{_CONTEXT_KEY: CAPTURE_STAMP_TOKEN}).write(
            {stamp_field: fields.Datetime.now()}
        )


def write_with_stamp(records, values, write, flag_field, stamp_field):
    """Run ``write`` and stamp companies whose flag value really changed."""

    values = strip_manual_stamp(records, values, stamp_field)
    if flag_field not in values:
        return write(values)
    new_value = bool(values[flag_field])
    changed = records.filtered(lambda company: bool(company[flag_field]) != new_value)
    result = write(values)
    stamp(changed, stamp_field)
    return result


def stamp_created(records, flag_field, stamp_field):
    stamp(records.filtered(lambda company: company[flag_field]), stamp_field)


def initialize_stamp(env, flag_field, stamp_field):
    """Upgrade step: enabled policies are verified from now on; others unknown."""

    companies = (
        env["res.company"]
        .sudo()
        .with_context(active_test=False)
        .search([(flag_field, "=", True), (stamp_field, "=", False)])
    )
    stamp(companies, stamp_field)
    return len(companies)
