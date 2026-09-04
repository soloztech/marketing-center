import datetime
import threading
import uuid

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..services.catalog_dto import SyncPageDTO
from ..services.performance_dto import PerformancePageDTO


@tagged("-at_install", "post_install")
class TestMarketingSyncConcurrency(TransactionCase):
    WORKER_TIMEOUT_SECONDS = 15

    def _setup_committed_fixture(self, *, create_cursor=False, sync_kind="catalog"):
        suffix = str(uuid.uuid4().int)[-12:]
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env["res.company"].browse(self.env.company.id)
            source = env["marketing.center.source"].create(
                {
                    "name": "Concurrent sync source %s" % suffix,
                    "company_id": company.id,
                    # Keep the base concurrency contract provider-neutral.  A
                    # post-install suite loads provider extensions too, and a
                    # real provider key would correctly activate their binding
                    # invariants (for example Meta requires a Meta profile).
                    "service": "test.concurrent",
                    "external_account_ref": "act_concurrent_%s" % suffix,
                    "currency_id": company.currency_id.id,
                    "timezone": "UTC",
                    "state": "active",
                }
            )
            connection = env["marketing.center.connection"].create(
                {
                    "name": "Concurrent sync reader %s" % suffix,
                    "source_id": source.id,
                    "adapter_key": "test.concurrent",
                    "purpose": "reader",
                    "profile_public_ref": "profile-concurrent-%s" % suffix,
                    "profile_revision": 1,
                    "state": "ready",
                }
            )
            fixture = {
                "company_id": company.id,
                "source_id": source.id,
                "connection_id": connection.id,
                "suffix": suffix,
                "sync_kind": sync_kind,
            }
            if create_cursor:
                run = self._plan_run(
                    env,
                    fixture,
                    trigger_ref="cursor-fixture-%s" % suffix,
                )
                cursor = env["marketing.center.sync.service"]._locked_cursor(run)
                fixture.update({"run_id": run.id, "cursor_id": cursor.id})
            cr.commit()  # pylint: disable=invalid-commit
            return fixture

    def _cleanup_committed_fixture(self, fixture):
        with self.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM marketing_center_sync_cursor WHERE source_id = %s",
                [fixture["source_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_sync_run WHERE source_id = %s",
                [fixture["source_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_connection WHERE source_id = %s",
                [fixture["source_id"]],
            )
            cr.execute(
                "DELETE FROM marketing_center_source WHERE id = %s",
                [fixture["source_id"]],
            )
            cr.commit()  # pylint: disable=invalid-commit

    @staticmethod
    def _plan_run(env, fixture, *, trigger_ref):
        if fixture["sync_kind"] == "metrics":
            return env["marketing.center.sync.service"]._plan_run(
                env["res.company"].browse(fixture["company_id"]),
                env["marketing.center.source"].browse(fixture["source_id"]),
                env["marketing.center.connection"].browse(fixture["connection_id"]),
                sync_kind="metrics",
                grain="campaign",
                scope_ref="account:act_concurrent_%s:campaign" % fixture["suffix"],
                trigger_kind="manual",
                trigger_ref=trigger_ref,
                reporting_context={
                    "contract_version": "test.concurrent.performance.daily.v1",
                    "grain": "campaign",
                    "currency": env["res.company"]
                    .browse(fixture["company_id"])
                    .currency_id.name,
                    "report_timezone": "UTC",
                    "level": "campaign",
                    "time_increment": 1,
                },
                window_start=datetime.datetime(2026, 9, 1, 0, 0),
                window_end=datetime.datetime(2026, 9, 2, 0, 0),
                report_timezone="UTC",
            )
        return env["marketing.center.sync.service"]._plan_run(
            env["res.company"].browse(fixture["company_id"]),
            env["marketing.center.source"].browse(fixture["source_id"]),
            env["marketing.center.connection"].browse(fixture["connection_id"]),
            sync_kind="catalog",
            entity_type="campaign",
            scope_ref="account:act_concurrent_%s" % fixture["suffix"],
            trigger_kind="manual",
            trigger_ref=trigger_ref,
            reporting_context={"catalog_version": 1},
        )

    @staticmethod
    def _same_page(run):
        page_class = PerformancePageDTO if run.sync_kind == "metrics" else SyncPageDTO
        return page_class(
            reporting_context_hash=run.reporting_context_hash,
            next_cursor="same-page-successor",
            has_more=True,
        )

    @staticmethod
    def _apply_same_page(env, run, page):
        service = env["marketing.center.sync.service"]
        if run.sync_kind == "metrics":
            return service._apply_performance_page(
                run,
                page,
                expected_cursor_sequence=0,
            )
        return service._apply_entity_page(
            run,
            page,
            expected_cursor_sequence=0,
        )

    def _plan_in_worker(
        self,
        fixture,
        trigger_ref,
        ready,
        start,
        outcomes,
        errors,
        terminalize_created=False,
    ):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                cr.execute(
                    "SELECT COUNT(*) FROM marketing_center_sync_run "
                    "WHERE source_id = %s",
                    [fixture["source_id"]],
                )
                if cr.fetchone()[0]:
                    raise RuntimeError("The concurrent fixture was not empty.")
                ready.set()
                if not start.wait(timeout=self.WORKER_TIMEOUT_SECONDS):
                    raise RuntimeError("Concurrent planners were not released.")
                try:
                    run = self._plan_run(
                        env,
                        fixture,
                        trigger_ref=trigger_ref,
                    )
                    if terminalize_created:
                        env["marketing.center.sync.service"]._cancel_run(run)
                    cr.commit()  # pylint: disable=invalid-commit
                    outcomes.append(("created", run.id))
                except ValidationError as error:
                    cr.rollback()
                    outcomes.append(("rejected", str(error)))
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)
            ready.set()

    def _run_concurrent_planners(
        self,
        fixture,
        trigger_refs,
        *,
        terminalize_created=False,
    ):
        ready = [threading.Event(), threading.Event()]
        start = threading.Event()
        outcomes = []
        errors = []
        workers = [
            threading.Thread(
                target=self._plan_in_worker,
                args=(
                    fixture,
                    trigger_refs[worker_number],
                    ready[worker_number],
                    start,
                    outcomes,
                    errors,
                    terminalize_created,
                ),
                name="marketing-sync-planner-%s" % worker_number,
                daemon=True,
            )
            for worker_number in range(2)
        ]
        try:
            for worker in workers:
                worker.start()
            self.assertTrue(
                all(event.wait(timeout=self.WORKER_TIMEOUT_SECONDS) for event in ready),
                "The concurrent planners did not become ready.",
            )
            start.set()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertFalse(
                any(worker.is_alive() for worker in workers),
                "A concurrent planner did not finish.",
            )
            if errors:
                raise errors[0]
            return outcomes
        finally:
            start.set()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)

    def test_only_one_concurrent_planner_owns_an_active_scope(self):
        fixture = self._setup_committed_fixture()
        trigger_refs = tuple(
            "planner-%s-%s" % (worker_number, fixture["suffix"])
            for worker_number in range(2)
        )
        try:
            outcomes = self._run_concurrent_planners(fixture, trigger_refs)
            self.assertEqual(
                sorted(outcome[0] for outcome in outcomes),
                ["created", "rejected"],
            )
            rejected = next(value for state, value in outcomes if state == "rejected")
            self.assertEqual(
                rejected,
                "Another synchronization already owns this source scope.",
            )
            with self.registry.cursor() as cr:
                cr.execute(
                    "SELECT COUNT(*) FROM marketing_center_sync_run "
                    "WHERE source_id = %s "
                    "AND state IN ('planned', 'queued', 'running')",
                    [fixture["source_id"]],
                )
                self.assertEqual(cr.fetchone()[0], 1)
        finally:
            self._cleanup_committed_fixture(fixture)

    def test_same_occurrence_concurrent_planners_are_rejected_safely(self):
        fixture = self._setup_committed_fixture()
        trigger_ref = "same-occurrence-%s" % fixture["suffix"]
        try:
            outcomes = self._run_concurrent_planners(
                fixture,
                (trigger_ref, trigger_ref),
                terminalize_created=True,
            )
            self.assertEqual(
                sorted(outcome[0] for outcome in outcomes),
                ["created", "rejected"],
            )
            rejected = next(value for state, value in outcomes if state == "rejected")
            self.assertEqual(
                rejected,
                "This synchronization occurrence was created concurrently.",
            )
            with self.registry.cursor() as cr:
                cr.execute(
                    "SELECT state FROM marketing_center_sync_run "
                    "WHERE source_id = %s",
                    [fixture["source_id"]],
                )
                self.assertEqual(cr.fetchall(), [("cancelled",)])
        finally:
            self._cleanup_committed_fixture(fixture)

    def _advance_cursor_in_worker(
        self,
        fixture,
        cached,
        row_locked,
        reader_attempting,
        errors,
    ):
        try:
            if not cached.wait(timeout=self.WORKER_TIMEOUT_SECONDS):
                raise RuntimeError("The cursor reader did not preload its cache.")
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                cr.execute(
                    "SELECT id FROM marketing_center_sync_cursor "
                    "WHERE id = %s FOR UPDATE",
                    [fixture["cursor_id"]],
                )
                cr.execute(
                    "UPDATE marketing_center_sync_cursor "
                    "SET cursor_sequence = 1, cursor_value = %s WHERE id = %s",
                    ["committed-next-page", fixture["cursor_id"]],
                )
                row_locked.set()
                if not reader_attempting.wait(timeout=self.WORKER_TIMEOUT_SECONDS):
                    raise RuntimeError("The cached reader did not request the lock.")
                cr.commit()  # pylint: disable=invalid-commit
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)
            row_locked.set()

    def _read_locked_cursor_in_worker(
        self,
        fixture,
        cached,
        row_locked,
        reader_attempting,
        observations,
        errors,
    ):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                run = env["marketing.center.sync.run"].browse(fixture["run_id"])
                cursor = env["marketing.center.sync.cursor"].browse(
                    fixture["cursor_id"]
                )
                observations.append(("preloaded", cursor.cursor_sequence))
                cached.set()
                if not row_locked.wait(timeout=self.WORKER_TIMEOUT_SECONDS):
                    raise RuntimeError("The cursor writer did not acquire its lock.")
                reader_attempting.set()
                try:
                    env["marketing.center.sync.service"]._locked_cursor(run)
                except SerializationFailure:
                    # Odoo uses REPEATABLE READ: once this transaction cached the
                    # old row, PostgreSQL must reject a lock after a concurrent
                    # commit. A queue attempt must roll back and retry from a new
                    # transaction instead of applying with a stale cursor.
                    cr.rollback()
                    observations.append(("retry_required",))
                else:
                    raise AssertionError(
                        "A stale REPEATABLE READ transaction was not rejected."
                    )
            with self.registry.cursor() as retry_cr:
                retry_env = api.Environment(retry_cr, SUPERUSER_ID, {})
                retry_run = retry_env["marketing.center.sync.run"].browse(
                    fixture["run_id"]
                )
                locked_cursor = retry_env[
                    "marketing.center.sync.service"
                ]._locked_cursor(retry_run)
                observations.append(
                    (
                        "locked_after_retry",
                        locked_cursor.cursor_sequence,
                        locked_cursor.cursor_value,
                    )
                )
                retry_cr.rollback()
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)
            cached.set()
            reader_attempting.set()

    def _apply_same_page_in_first_worker(
        self,
        fixture,
        run_locked,
        second_started,
        outcomes,
        errors,
    ):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                run = env["marketing.center.sync.run"].browse(fixture["run_id"])
                cr.execute(
                    "SELECT id FROM marketing_center_sync_run "
                    "WHERE id = %s FOR UPDATE",
                    [run.id],
                )
                page = self._same_page(run)
                run_locked.set()
                if not second_started.wait(timeout=self.WORKER_TIMEOUT_SECONDS):
                    raise RuntimeError("The second page worker did not start.")
                self._apply_same_page(env, run, page)
                cr.commit()  # pylint: disable=invalid-commit
                outcomes.append(("applied",))
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)
            run_locked.set()

    def _apply_same_page_in_second_worker(
        self,
        fixture,
        run_locked,
        second_started,
        outcomes,
        errors,
    ):
        try:
            if not run_locked.wait(timeout=self.WORKER_TIMEOUT_SECONDS):
                raise RuntimeError("The first page worker did not lock the run.")
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                run = env["marketing.center.sync.run"].browse(fixture["run_id"])
                page = self._same_page(run)
                self.assertEqual(run.state, "planned")
                second_started.set()
                try:
                    self._apply_same_page(env, run, page)
                except SerializationFailure:
                    cr.rollback()
                    outcomes.append(("retry_required",))
                else:
                    raise AssertionError(
                        "A concurrent page worker did not request a transaction retry."
                    )
            with self.registry.cursor() as retry_cr:
                retry_env = api.Environment(retry_cr, SUPERUSER_ID, {})
                retry_run = retry_env["marketing.center.sync.run"].browse(
                    fixture["run_id"]
                )
                retry_page = self._same_page(retry_run)
                try:
                    self._apply_same_page(retry_env, retry_run, retry_page)
                except ValidationError as error:
                    retry_cr.rollback()
                    outcomes.append(("cas_rejected", str(error)))
                else:
                    raise AssertionError(
                        "The same synchronization page was applied twice."
                    )
        except Exception as error:  # surface worker failures in the main thread
            errors.append(error)
            second_started.set()

    def _assert_same_page_is_applied_once(self, sync_kind):
        fixture = self._setup_committed_fixture(
            create_cursor=True,
            sync_kind=sync_kind,
        )
        run_locked = threading.Event()
        second_started = threading.Event()
        outcomes = []
        errors = []
        workers = (
            threading.Thread(
                target=self._apply_same_page_in_first_worker,
                args=(
                    fixture,
                    run_locked,
                    second_started,
                    outcomes,
                    errors,
                ),
                name="marketing-sync-first-page-worker",
                daemon=True,
            ),
            threading.Thread(
                target=self._apply_same_page_in_second_worker,
                args=(
                    fixture,
                    run_locked,
                    second_started,
                    outcomes,
                    errors,
                ),
                name="marketing-sync-second-page-worker",
                daemon=True,
            ),
        )
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertFalse(
                any(worker.is_alive() for worker in workers),
                "A concurrent page worker did not finish.",
            )
            if errors:
                raise errors[0]
            self.assertEqual(
                {outcome[0] for outcome in outcomes},
                {"applied", "retry_required", "cas_rejected"},
            )
            rejection = next(
                outcome[1] for outcome in outcomes if outcome[0] == "cas_rejected"
            )
            self.assertEqual(
                rejection,
                "The synchronization cursor changed concurrently.",
            )
            with self.registry.cursor() as cr:
                cr.execute(
                    "SELECT state, page_count FROM marketing_center_sync_run "
                    "WHERE id = %s",
                    [fixture["run_id"]],
                )
                self.assertEqual(cr.fetchone(), ("running", 1))
                cr.execute(
                    "SELECT cursor_sequence, cursor_value "
                    "FROM marketing_center_sync_cursor WHERE id = %s",
                    [fixture["cursor_id"]],
                )
                self.assertEqual(cr.fetchone(), (1, "same-page-successor"))
        finally:
            run_locked.set()
            second_started.set()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self._cleanup_committed_fixture(fixture)

    def test_same_catalog_page_is_applied_once_by_concurrent_workers(self):
        self._assert_same_page_is_applied_once("catalog")

    def test_same_performance_page_is_applied_once_by_concurrent_workers(self):
        self._assert_same_page_is_applied_once("metrics")

    def test_locked_cursor_retries_after_a_concurrent_committed_advance(self):
        fixture = self._setup_committed_fixture(create_cursor=True)
        cached = threading.Event()
        row_locked = threading.Event()
        reader_attempting = threading.Event()
        observations = []
        errors = []
        reader = threading.Thread(
            target=self._read_locked_cursor_in_worker,
            args=(
                fixture,
                cached,
                row_locked,
                reader_attempting,
                observations,
                errors,
            ),
            name="marketing-sync-cached-cursor-reader",
            daemon=True,
        )
        writer = threading.Thread(
            target=self._advance_cursor_in_worker,
            args=(
                fixture,
                cached,
                row_locked,
                reader_attempting,
                errors,
            ),
            name="marketing-sync-cursor-writer",
            daemon=True,
        )
        try:
            reader.start()
            writer.start()
            reader.join(self.WORKER_TIMEOUT_SECONDS)
            writer.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertFalse(
                reader.is_alive(), "The cached cursor reader did not finish."
            )
            self.assertFalse(writer.is_alive(), "The cursor writer did not finish.")
            if errors:
                raise errors[0]
            self.assertEqual(
                observations,
                [
                    ("preloaded", 0),
                    ("retry_required",),
                    ("locked_after_retry", 1, "committed-next-page"),
                ],
            )
        finally:
            cached.set()
            row_locked.set()
            reader_attempting.set()
            reader.join(self.WORKER_TIMEOUT_SECONDS)
            writer.join(self.WORKER_TIMEOUT_SECONDS)
            self._cleanup_committed_fixture(fixture)
