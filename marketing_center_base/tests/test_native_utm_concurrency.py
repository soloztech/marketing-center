import datetime
import threading
import uuid
from dataclasses import replace
from unittest.mock import patch

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from ..models.attribution_resolution import ASSET_RESOLUTION_WRITE_TOKEN
from ..services.catalog_dto import ExternalEntityDTO
from ..services.dto import MarketingTouchpointDTO


@tagged("-at_install", "post_install")
class TestMarketingNativeUtmConcurrency(TransactionCase):
    def test_stale_resolution_retries_after_accepted_revision(self):
        self._assert_stale_resolution_retries(projection_fails=False)

    @mute_logger(
        "odoo.addons.marketing_center_base.models.attribution_resolution_service"
    )
    def test_stale_resolution_retries_after_projection_failure(self):
        self._assert_stale_resolution_retries(projection_fails=True)

    def _assert_stale_resolution_retries(self, projection_fails):
        suffix = uuid.uuid4().hex
        company_id = self.env.company.id
        context = {"allowed_company_ids": [company_id]}
        fixture = {}
        dto = MarketingTouchpointDTO(
            source_system="test.native.utm.race",
            source_scope_ref=suffix,
            source_occurrence_ref="revision-race",
            source_evidence_ref="original",
            occurred_at=datetime.datetime(2026, 9, 15, 12),
            platform="test",
            channel="test",
            touchpoint_type="unknown",
            evidence_level="provider_asserted",
            asset_refs={"test.campaign_id": suffix + "/campaign"},
        )
        # Downstream CRM callbacks may enqueue jobs. Keep these committed
        # synthetic fixtures isolated from unrelated reconciliation workers.
        with patch.object(
            type(self.env["marketing.native.utm.service"]),
            "_after_native_utm_change",
            return_value=True,
        ):
            try:
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL statement_timeout = '5s'")
                    env = api.Environment(cr, SUPERUSER_ID, context)
                    native_source = env["utm.source"].create(
                        {"name": "Revision race source " + suffix}
                    )
                    native_medium = env["utm.medium"].create(
                        {"name": "Revision race medium " + suffix}
                    )
                    source = env["marketing.center.source"].create(
                        {
                            "name": "Revision race " + suffix,
                            "company_id": company_id,
                            "service": "test.ads",
                            "external_account_ref": suffix,
                            "state": "active",
                            "native_utm_mode": "apply",
                            "native_utm_source_id": native_source.id,
                            "native_utm_medium_id": native_medium.id,
                        }
                    )
                    catalog = env["marketing.center.catalog.service"]._upsert_entity(
                        source.company_id,
                        source,
                        ExternalEntityDTO(
                            entity_type="campaign",
                            external_ref=suffix + "/campaign",
                            name="Revision race campaign " + suffix,
                            observed_at=datetime.datetime(2026, 9, 15, 12),
                        ),
                    )
                    entity = env["marketing.center.external.entity"].browse(
                        catalog.entity_id
                    )
                    original = env["marketing.attribution.service"]._ingest_touchpoint(
                        source.company_id, dto
                    )
                    resolution = env["marketing.attribution.asset.resolution"].search(
                        [("touchpoint_id", "=", original.touchpoint_id)]
                    )
                    self.assertEqual(len(resolution), 1)
                    resolution.with_context(
                        marketing_asset_resolution_write_token=ASSET_RESOLUTION_WRITE_TOKEN
                    ).write(
                        {
                            "target_kind": "entity",
                            "provider_key": "test",
                            "service_key": source.service,
                            "mapped_entity_type": "campaign",
                            "canonical_external_ref": entity.external_ref,
                            "source_id": source.id,
                            "entity_id": entity.id,
                            "state": "resolved",
                            "reason": "test_explicit_identity",
                        }
                    )
                    # Commit the native mapping before either race participant
                    # starts. Only the asset resolution changes in the writer.
                    native = env["marketing.native.utm.service"]._resolve_entity(
                        entity, apply=True
                    )
                    fixture.update(
                        source_id=source.id,
                        entity_id=entity.id,
                        campaign_id=native["campaign_id"],
                        native_source_id=native_source.id,
                        native_medium_id=native_medium.id,
                        touchpoint_id=original.touchpoint_id,
                        resolution_id=resolution.id,
                    )
                    cr.commit()  # pylint: disable=invalid-commit

                # Independent cursors give deterministic overlapping snapshots;
                # no thread or timing-dependent lock contention is needed.
                with self.registry.cursor() as stale_cr:
                    stale_cr.execute("SET LOCAL statement_timeout = '5s'")
                    stale_env = api.Environment(stale_cr, SUPERUSER_ID, context)
                    old_point = stale_env["marketing.attribution.touchpoint"].browse(
                        fixture["touchpoint_id"]
                    )
                    before = stale_env[
                        "marketing.native.utm.service"
                    ]._resolve_touchpoint(old_point)
                    self.assertEqual(before["state"], "ready")
                    self.assertEqual(before["campaign_id"], fixture["campaign_id"])

                    with self.registry.cursor() as writer_cr:
                        writer_cr.execute("SET LOCAL statement_timeout = '5s'")
                        writer_env = api.Environment(writer_cr, SUPERUSER_ID, context)
                        ingest = writer_env["marketing.attribution.service"]
                        company = writer_env["res.company"].browse(company_id)
                        revised_dto = replace(
                            dto,
                            source_evidence_ref="correction",
                            revision_kind="correction",
                            asset_refs={},
                        )
                        if projection_fails:
                            resolver = writer_env[
                                "marketing.attribution.asset.resolution.service"
                            ]
                            with patch.object(
                                type(resolver),
                                "_project_effective_touchpoint",
                                side_effect=RuntimeError(
                                    "Synthetic projection failure"
                                ),
                            ) as project:
                                revised = ingest._ingest_touchpoint(
                                    company, revised_dto
                                )
                            project.assert_called_once()
                        else:
                            revised = ingest._ingest_touchpoint(company, revised_dto)
                        self.assertEqual(revised.disposition, "revised")
                        self.assertNotEqual(
                            revised.touchpoint_id, fixture["touchpoint_id"]
                        )
                        surviving = (
                            writer_env["marketing.attribution.asset.resolution"]
                            .browse(fixture["resolution_id"])
                            .exists()
                        )
                        if projection_fails:
                            self.assertTrue(surviving)
                            self.assertEqual(
                                surviving.touchpoint_id.id, fixture["touchpoint_id"]
                            )
                            self.assertEqual(surviving.state, "resolved")
                            self.assertEqual(
                                surviving.entity_id.id, fixture["entity_id"]
                            )
                        else:
                            self.assertFalse(surviving)
                        writer_cr.commit()  # pylint: disable=invalid-commit

                    # The advisory locks are now available, but the old MVCC
                    # snapshot still sees the previously accepted resolution.
                    # Applying it must request a whole-transaction retry.
                    with self.assertRaises(SerializationFailure):
                        stale_env["marketing.native.utm.service"]._resolve_touchpoint(
                            old_point, apply=True
                        )
                    stale_cr.rollback()

                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL statement_timeout = '5s'")
                    env = api.Environment(cr, SUPERUSER_ID, context)
                    effective = env[
                        "marketing.attribution.effective.touchpoint"
                    ].search(
                        [
                            ("company_id", "=", company_id),
                            ("canonical_key", "=", dto.canonical_key),
                        ]
                    )
                    self.assertEqual(effective.id, revised.touchpoint_id)
                    service = env["marketing.native.utm.service"]
                    replay = service._resolve_touchpoint(
                        env["marketing.attribution.touchpoint"].browse(
                            fixture["touchpoint_id"]
                        ),
                        apply=True,
                    )
                    self.assertEqual(replay["reason"], "effective_revision_required")
                    current = service._resolve_touchpoint(effective, apply=True)
                    self.assertEqual(current["state"], "missing")
                    self.assertFalse(current["campaign_id"])
                    self.assertFalse(current["created"])
            finally:
                if fixture:
                    self._cleanup_revision_fixture(
                        fixture, company_id, dto.canonical_key
                    )

    def _cleanup_revision_fixture(self, fixture, company_id, canonical_key):
        # Remove only this test's committed synthetic ledger and catalog rows.
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL statement_timeout = '5s'")
            cr.execute(
                "SELECT id FROM marketing_attribution_touchpoint "
                "WHERE company_id = %s AND canonical_key = %s",
                [company_id, canonical_key],
            )
            touchpoint_ids = [row[0] for row in cr.fetchall()]
            cr.execute(
                "DELETE FROM marketing_attribution_asset_resolution "
                "WHERE touchpoint_id = ANY(%s)",
                [touchpoint_ids],
            )
            cr.execute(
                "DELETE FROM marketing_attribution_identifier "
                "WHERE touchpoint_id = ANY(%s)",
                [touchpoint_ids],
            )
            cr.execute(
                "DELETE FROM marketing_attribution_evidence "
                "WHERE touchpoint_id = ANY(%s)",
                [touchpoint_ids],
            )
            cr.execute(
                "DELETE FROM marketing_attribution_touchpoint WHERE id = ANY(%s)",
                [touchpoint_ids],
            )
            cr.execute(
                "UPDATE marketing_center_external_entity "
                "SET current_revision_id = NULL WHERE id = %s",
                [fixture["entity_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_external_entity_revision WHERE entity_id = %s",
                [fixture["entity_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_external_entity WHERE id = %s",
                [fixture["entity_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_source WHERE id = %s",
                [fixture["source_id"]],
            )
            cr.execute(
                "DELETE FROM utm_campaign WHERE id = %s", [fixture["campaign_id"]]
            )
            cr.execute(
                "DELETE FROM utm_source WHERE id = %s", [fixture["native_source_id"]]
            )
            cr.execute(
                "DELETE FROM utm_medium WHERE id = %s", [fixture["native_medium_id"]]
            )
            cr.commit()  # pylint: disable=invalid-commit

    def test_concurrent_creation_retries_and_keeps_one_campaign(self):
        """The waiter has a snapshot older than the writer's committed mapping."""
        suffix = uuid.uuid4().hex
        fixture = {}
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                native_source = env["utm.source"].create(
                    {"name": "Race source " + suffix}
                )
                native_medium = env["utm.medium"].create(
                    {"name": "Race medium " + suffix}
                )
                source = env["marketing.center.source"].create(
                    {
                        "name": "Race " + suffix,
                        "service": "test.ads",
                        "external_account_ref": suffix,
                        "state": "active",
                        "native_utm_mode": "apply",
                        "native_utm_source_id": native_source.id,
                        "native_utm_medium_id": native_medium.id,
                    }
                )
                result = env["marketing.center.catalog.service"]._upsert_entity(
                    source.company_id,
                    source,
                    ExternalEntityDTO(
                        entity_type="campaign",
                        external_ref=suffix + "/campaign",
                        name="Race campaign " + suffix,
                        observed_at=datetime.datetime(2026, 9, 15, 12),
                    ),
                )
                fixture.update(
                    source_id=source.id,
                    entity_id=result.entity_id,
                    native_source_id=native_source.id,
                    native_medium_id=native_medium.id,
                )
                cr.commit()  # pylint: disable=invalid-commit
            snapshot_ready = threading.Event()
            continue_waiter = threading.Event()
            errors = []

            def waiter():
                try:
                    with self.registry.cursor() as cr:
                        cr.execute("SET LOCAL statement_timeout = '10s'")
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        entity = env["marketing.center.external.entity"].browse(
                            fixture["entity_id"]
                        )
                        # Materialize the old repeatable-read snapshot before the
                        # main transaction writes and commits the association.
                        self.assertFalse(entity.native_utm_campaign_id)
                        snapshot_ready.set()
                        if not continue_waiter.wait(10):
                            raise RuntimeError("Native mapping writer did not finish")
                        env["marketing.native.utm.service"]._resolve_entity(
                            entity, apply=True
                        )
                        cr.commit()  # pylint: disable=invalid-commit
                except Exception as error:  # thread errors must reach the test
                    errors.append(error)

            worker = threading.Thread(target=waiter, daemon=True)
            worker.start()
            self.assertTrue(snapshot_ready.wait(10))
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                result = env["marketing.native.utm.service"]._resolve_entity(
                    env["marketing.center.external.entity"].browse(
                        fixture["entity_id"]
                    ),
                    apply=True,
                )
                fixture["campaign_id"] = result["campaign_id"]
                cr.commit()  # pylint: disable=invalid-commit
            continue_waiter.set()
            worker.join(12)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], SerializationFailure)
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                replay = env["marketing.native.utm.service"]._resolve_entity(
                    env["marketing.center.external.entity"].browse(
                        fixture["entity_id"]
                    ),
                    apply=True,
                )
                self.assertEqual(replay["campaign_id"], fixture["campaign_id"])
                self.assertFalse(replay["created"])
                self.assertEqual(
                    env["utm.campaign"].search_count(
                        [("title", "=", "Race campaign " + suffix)]
                    ),
                    1,
                )
        finally:
            # Only committed fixtures from this isolated test are removed.
            if fixture:
                with self.registry.cursor() as cr:
                    cr.execute(
                        "UPDATE marketing_center_external_entity SET current_revision_id = NULL WHERE id = %s",
                        [fixture["entity_id"]],
                    )
                    cr.execute(
                        "DELETE FROM marketing_center_external_entity_revision WHERE entity_id = %s",
                        [fixture["entity_id"]],
                    )
                    cr.execute(
                        "DELETE FROM marketing_center_external_entity WHERE id = %s",
                        [fixture["entity_id"]],
                    )
                    cr.execute(
                        "DELETE FROM marketing_center_source WHERE id = %s",
                        [fixture["source_id"]],
                    )
                    if fixture.get("campaign_id"):
                        cr.execute(
                            "DELETE FROM utm_campaign WHERE id = %s",
                            [fixture["campaign_id"]],
                        )
                    cr.execute(
                        "DELETE FROM utm_source WHERE id = %s",
                        [fixture["native_source_id"]],
                    )
                    cr.execute(
                        "DELETE FROM utm_medium WHERE id = %s",
                        [fixture["native_medium_id"]],
                    )
                    cr.commit()  # pylint: disable=invalid-commit
