import re

from odoo import _
from odoo.exceptions import ValidationError

_CURSOR_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")


def fair_scheduler_batch(env, model_name, domain, *, cursor_key, limit):
    """Select one serial, bounded round-robin batch for a technical cron.

    The cursor and the caller's queued work live in the same transaction. A
    rollback therefore repeats the batch (at-least-once), while the advisory
    lock prevents concurrent invocations of the same lane from advancing from
    one stale cursor snapshot.
    """

    if not isinstance(model_name, str) or not _MODEL_NAME_RE.fullmatch(model_name):
        raise ValidationError(_("The scheduler model name is invalid."))
    if not isinstance(cursor_key, str) or not _CURSOR_KEY_RE.fullmatch(cursor_key):
        raise ValidationError(_("The scheduler cursor key is invalid."))
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValidationError(_("The scheduler batch limit is invalid."))
    if not isinstance(domain, (list, tuple)):
        raise ValidationError(_("The scheduler domain is invalid."))

    env.cr.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
        ["marketing.center.scheduler:%s" % model_name, cursor_key],
    )
    parameter = "marketing_center.scheduler_cursor.%s.%s" % (model_name, cursor_key)
    parameters = env["ir.config_parameter"].sudo()
    raw_cursor = parameters.get_param(parameter, "0")
    try:
        cursor_id = max(0, int(raw_cursor or 0))
    except (TypeError, ValueError):
        cursor_id = 0

    model = env[model_name].sudo()
    base_domain = list(domain)
    after = model.search(
        base_domain + [("id", ">", cursor_id)],
        order="id",
        limit=limit,
    )
    selected_ids = list(after.ids)
    remaining = limit - len(selected_ids)
    if remaining:
        wrapped = model.search(
            base_domain + [("id", "<=", cursor_id)],
            order="id",
            limit=remaining,
        )
        selected_ids.extend(wrapped.ids)
    batch = model.browse(selected_ids)
    if batch:
        parameters.set_param(parameter, str(batch[-1].id))
    return batch
