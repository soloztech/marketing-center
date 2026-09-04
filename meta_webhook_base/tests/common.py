import hashlib
import json
import os

from odoo.tests.common import SavepointCase

from ..services.sanitizer import sanitized_webhook
from ..services.tokens import META_WEBHOOK_INTERNAL_TOKEN


class MetaWebhookCase(SavepointCase):
    APP_SECRET_REF = "ODOO_META_WEBHOOK_TEST_APP_SECRET"
    VERIFY_TOKEN_REF = "ODOO_META_WEBHOOK_TEST_VERIFY_TOKEN"
    PAGE_TOKEN_REF = "ODOO_META_WEBHOOK_TEST_PAGE_TOKEN"
    APP_SECRET = "synthetic-app-secret-12345"
    VERIFY_TOKEN = "synthetic-verify-token-12345"
    PAGE_TOKEN = "synthetic-page-token-12345"
    PAGE_ID = "100000000000101"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._previous_environment = {
            key: os.environ.get(key)
            for key in (
                cls.APP_SECRET_REF,
                cls.VERIFY_TOKEN_REF,
                cls.PAGE_TOKEN_REF,
            )
        }
        os.environ[cls.APP_SECRET_REF] = cls.APP_SECRET
        os.environ[cls.VERIFY_TOKEN_REF] = cls.VERIFY_TOKEN
        os.environ[cls.PAGE_TOKEN_REF] = cls.PAGE_TOKEN
        cls.app = cls.env["meta.api.app"].create(
            {
                "name": "Webhook test App",
                "external_app_id": "100000000000001",
                "graph_version": "v26.0",
                "credential_backend": "environment",
                "app_secret_ref": cls.APP_SECRET_REF,
            }
        )
        cls.endpoint = cls.env["meta.webhook.endpoint"].create(
            {
                "name": "Webhook test endpoint",
                "app_id": cls.app.id,
                "credential_backend": "environment",
                "verify_token_ref": cls.VERIFY_TOKEN_REF,
            }
        )
        cls.page = cls.env["meta.webhook.page"].create(
            {
                "name": "Webhook test Page",
                "endpoint_id": cls.endpoint.id,
                "external_page_id": cls.PAGE_ID,
                "credential_backend": "environment",
                "access_token_ref": cls.PAGE_TOKEN_REF,
            }
        )

    @classmethod
    def tearDownClass(cls):
        for key, previous in cls._previous_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        super().tearDownClass()

    @classmethod
    def leadgen_envelope(cls, lead_id="200000000000001"):
        return {
            "object": "page",
            "entry": [
                {
                    "id": cls.PAGE_ID,
                    "time": 1_800_000_000,
                    "changes": [
                        {
                            "field": "leadgen",
                            "value": {
                                "leadgen_id": lead_id,
                                "page_id": cls.PAGE_ID,
                                "form_id": "300000000000001",
                                "created_time": 1_800_000_000,
                                "ad_id": "400000000000001",
                            },
                        }
                    ],
                }
            ],
        }

    @classmethod
    def messaging_envelope(cls, object_type="page", target=None):
        return {
            "object": object_type,
            "entry": [
                {
                    "id": target or cls.PAGE_ID,
                    "time": 1_800_000_000,
                    "messaging": [
                        {"sender": {"id": "999"}, "message": {"text": "raw"}}
                    ],
                }
            ],
        }

    def create_delivery(self, envelope):
        sanitized = sanitized_webhook(envelope)
        body = json.dumps(envelope, separators=(",", ":")).encode()
        delivery = (
            self.env["meta.webhook.delivery"]
            .sudo()
            .with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN)
            .create(
                {
                    "endpoint_id": self.endpoint.id,
                    "endpoint_revision": self.endpoint.revision,
                    "app_revision": self.app.revision,
                    "content_sha256": hashlib.sha256(body).hexdigest(),
                    "body_size_bytes": len(body),
                    "object_type": sanitized.object_type,
                    "graph_version": self.app.graph_version,
                    "sanitized_envelope_json": sanitized.envelope,
                }
            )
        )
        self.env["meta.webhook.dispatcher"]._ingest_delivery(
            self.endpoint, delivery, envelope, sanitized
        )
        return delivery
