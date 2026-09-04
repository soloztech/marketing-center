import json
import re

from odoo import _, api, models
from odoo.exceptions import ValidationError

from ..services.sanitizer import (
    MetaWebhookSanitizationError,
    canonical_digest,
    validate_sanitized_payload,
)
from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN

_ITEM_KEY_RE = re.compile(r"^entry:([0-9]{1,3}):messaging:([0-9]{1,3})$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ID_RE = re.compile(r"^[0-9]{1,40}$")
_MAX_ITEM_BYTES = 64 * 1024


class MetaWebhookDispatcher(models.AbstractModel):
    _name = "meta.webhook.dispatcher"
    _description = "Extensible Meta Webhook Dispatcher"

    @api.model
    def _consumer_item_specs(self, endpoint, delivery, decoded_envelope):
        """Return sanitized messaging item specs from installed consumers.

        Consumer addons must call ``super()`` and append only entries matching an
        ``entry:N:messaging:N`` carrier in the original bounded envelope. They may
        persist short-lived provider-private artifacts in the same transaction,
        while the shared immutable item must contain only their sanitized reference.
        """

        return ()

    @api.model
    def _ingest_delivery(self, endpoint, delivery, decoded_envelope, sanitized):
        endpoint.ensure_one()
        delivery.ensure_one()
        expected = set(sanitized.expected_messaging_keys)
        specs = list(sanitized.items)
        extension_specs = tuple(
            self._consumer_item_specs(endpoint, delivery, decoded_envelope) or ()
        )
        claimed = set()
        for incoming in extension_specs:
            spec = self._validated_messaging_spec(incoming, sanitized)
            if spec["item_key"] in claimed:
                raise ValidationError(_("A Meta messaging item was claimed twice."))
            claimed.add(spec["item_key"])
            specs.append(spec)
        if not claimed.issubset(expected):
            raise ValidationError(
                _("A consumer returned an item outside the Meta envelope.")
            )
        for item_key in sorted(expected - claimed):
            specs.append(self._messaging_placeholder(item_key, sanitized))
        if len({spec["item_key"] for spec in specs}) != len(specs):
            raise ValidationError(_("The Meta webhook item keys are inconsistent."))
        item_model = (
            self.env["meta.webhook.item"]
            .sudo()
            .with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN)
        )
        items = item_model.create(
            [
                dict(spec, delivery_id=delivery.id)
                for spec in sorted(specs, key=self._sort)
            ]
        )
        delivery.with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN).write(
            {"item_count": len(items)}
        )
        return items

    @api.model
    def _validated_messaging_spec(self, incoming, sanitized):
        if not isinstance(incoming, dict):
            raise ValidationError(_("A Meta consumer item must be an object."))
        item_key = str(incoming.get("item_key") or "")
        match = _ITEM_KEY_RE.fullmatch(item_key)
        if not match:
            raise ValidationError(_("A Meta consumer item key is invalid."))
        entry_index, item_index = (int(value) for value in match.groups())
        if item_key not in set(sanitized.expected_messaging_keys):
            raise ValidationError(_("A Meta consumer item is outside the envelope."))
        entry = sanitized.envelope["entry"][entry_index]
        payload = incoming.get("payload_json")
        if not isinstance(payload, dict):
            raise ValidationError(_("A Meta consumer payload must be an object."))
        try:
            validate_sanitized_payload(payload)
        except MetaWebhookSanitizationError:
            raise ValidationError(
                _("A Meta consumer payload is not safely sanitized.")
            ) from None
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ValidationError(
                _("A Meta consumer payload is not valid JSON.")
            ) from None
        if len(encoded) > _MAX_ITEM_BYTES:
            raise ValidationError(_("A Meta consumer payload is too large."))
        object_type = str(incoming.get("object_type") or "").strip().lower()
        event_field = str(incoming.get("event_field") or "").strip().lower()
        target_asset_id = str(incoming.get("target_asset_id") or "").strip()
        if object_type != sanitized.object_type or not _TOKEN_RE.fullmatch(event_field):
            raise ValidationError(_("A Meta consumer routing key is invalid."))
        if not _ID_RE.fullmatch(target_asset_id) or target_asset_id != entry["id"]:
            raise ValidationError(_("A Meta consumer target is invalid."))
        occurrence_ref = str(incoming.get("occurrence_ref") or "").strip()
        if (
            not occurrence_ref
            or len(occurrence_ref) > 256
            or any(ord(character) < 32 for character in occurrence_ref)
        ):
            raise ValidationError(_("A Meta consumer occurrence is invalid."))
        return {
            "sequence": entry_index * 1000 + 500 + item_index,
            "item_key": item_key,
            "kind": "messaging",
            "object_type": object_type,
            "event_field": event_field,
            "target_asset_id": target_asset_id,
            "occurrence_ref": occurrence_ref,
            "payload_json": payload,
            "event_sha256": canonical_digest(payload),
        }

    @api.model
    def _messaging_placeholder(self, item_key, sanitized):
        match = _ITEM_KEY_RE.fullmatch(item_key)
        entry_index, item_index = (int(value) for value in match.groups())
        entry = sanitized.envelope["entry"][entry_index]
        payload = {
            "schema_version": "meta.webhook.v1",
            "object": sanitized.object_type,
            "entry": {
                "id": entry["id"],
                **({"time": entry["time"]} if "time" in entry else {}),
            },
            "messaging_index": item_index,
            "reason": "consumer_unavailable",
        }
        return {
            "sequence": entry_index * 1000 + 500 + item_index,
            "item_key": item_key,
            "kind": "unknown",
            "object_type": sanitized.object_type,
            "event_field": "messages",
            "target_asset_id": entry["id"],
            "occurrence_ref": "messaging:%s:%s" % (entry["id"], item_index),
            "payload_json": payload,
            "event_sha256": canonical_digest(payload),
        }

    @api.model
    def _sort(self, spec):
        return spec["sequence"], spec["item_key"]

    @api.model
    def _dispatch_consumer(self, dispatch):
        """Process one consumer dispatch or return ``None`` when unclaimed.

        An installed consumer returns ``{"handled": True, "result_ref": "..."}``
        only after its local artifact is durable in the current transaction.
        """

        return None

    @api.model
    def _dispatch_retry_limit_error_class(self, dispatch, error):
        """Return a safe terminal reason for an exhausted consumer retry.

        Consumer addons may refine the generic reason for a private exception
        type. The dispatch worker validates the returned token before storing it,
        so exception text and provider secrets never enter the shared ledger.
        """

        return "RetryLimit"

    @api.model
    def _after_subscription_reconcile(self, endpoint):
        """Allow consumers to revive work gated on a successful readback.

        This generic hook deliberately knows nothing about any consumer model.
        Implementations must recover only their own explicitly classified work.
        """

        return True

    @api.model
    def _validated_dispatch_result(self, value):
        if not isinstance(value, dict) or value.get("handled") is not True:
            raise ValidationError(_("The Meta consumer result is invalid."))
        result_ref = str(value.get("result_ref") or "").strip()
        if len(result_ref) > 512 or any(
            ord(character) < 32 for character in result_ref
        ):
            raise ValidationError(_("The Meta consumer result reference is invalid."))
        return {"handled": True, "result_ref": result_ref}
