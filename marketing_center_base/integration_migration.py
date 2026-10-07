"""Reversible metadata preparation for the verified CRM integration lineage.

Call with the predecessor registry, while services are stopped. The caller owns
the durable receipt, acknowledgement, commit and source promotion. No business
record is deleted, and this module never commits or signals registry changes.
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from odoo import _
from odoo.exceptions import ValidationError

OWNERS = {
    "marketing_center_crm": "marketing_center_base",
    "marketing_center_dashboard": "marketing_center_base",
    "marketing_center_meta_crm": "marketing_center_meta",
    "marketing_center_contact_center_crm": "marketing_center_contact_center",
    "marketing_center_website_crm": "marketing_center_website",
}
LEGACY_VERSIONS = {
    "marketing_center_crm": "16.0.2.0.0",
    "marketing_center_dashboard": "16.0.2.0.0",
    "marketing_center_meta_crm": "16.0.1.1.1",
    "marketing_center_contact_center_crm": "16.0.1.0.2",
    "marketing_center_website_crm": "16.0.1.6.2",
}
PREDECESSOR_VERSIONS = {
    "marketing_center_base": "16.0.2.0.0",
    "marketing_center_meta": "16.0.1.1.5",
    "marketing_center_contact_center": "16.0.1.3.0",
    "marketing_center_website": "16.0.1.6.0",
    "marketing_center_website_whatsapp": "16.0.1.2.0",
}
VERSIONS = {
    "marketing_center_base": "16.0.2.1.0",
    "marketing_center_meta": "16.0.2.0.0",
    "marketing_center_contact_center": "16.0.2.0.0",
    "marketing_center_website": "16.0.2.0.0",
    "marketing_center_website_whatsapp": "16.0.1.3.0",
}
# Checkpoints above identify the original ownership migration. Supported later
# releases must be accepted both during prepared upgrades and on fresh graphs.
SUCCESSOR_VERSIONS = {
    "marketing_center_base": ("16.0.2.1.1", "16.0.2.1.2"),
    "marketing_center_contact_center": ("16.0.2.0.1",),
    "marketing_center_website": ("16.0.2.1.0", "16.0.2.1.1"),
    "marketing_center_website_whatsapp": ("16.0.1.3.1", "16.0.1.4.0", "16.0.1.5.0"),
}
DEPENDENCIES = {
    "marketing_center_base": ("base", "crm", "queue_job", "utm"),
    "marketing_center_meta": (
        "marketing_center_base",
        "meta_api_base",
        "meta_webhook_base",
        "queue_job",
    ),
    "marketing_center_contact_center": (
        "contact_center_base",
        "contact_center_crm",
        "marketing_center_base",
        "queue_job",
    ),
    "marketing_center_website": (
        "marketing_center_base",
        "marketing_center_web_ingress",
        "queue_job",
        "website",
        "website_crm",
    ),
    "marketing_center_website_whatsapp": (
        "contact_center_crm",
        "marketing_center_website",
    ),
}
SUITE = "marketing_center_suite"
TRANSIENT = ("to install", "to upgrade", "to remove")
SCHEMA_MODELS = ("ir.model.constraint", "ir.model.relation")
MARKER_PREFIX = "marketing_center.integration_fusion."


def _require(condition, message):
    if not condition:
        raise ValidationError(
            _("Marketing CRM integration fusion: %(detail)s") % {"detail": message}
        )


@lru_cache(maxsize=1)
def catalog():
    return json.loads(
        (Path(__file__).parent / "data/integration_legacy_catalog.json").read_text()
    )


def markers(env):
    parameters = env["ir.config_parameter"].sudo()
    return {
        name: parameters.get_param(MARKER_PREFIX + name, False) for name in VERSIONS
    }


def checkpoint(env, owner):
    """Post-migration: this write must precede the loader's data-stage commit."""
    _require(owner in VERSIONS, "unknown checkpoint owner")
    assert_prepared(env)
    env["ir.config_parameter"].sudo().set_param(MARKER_PREFIX + owner, VERSIONS[owner])


def _all_module_states(env):
    return (
        env["ir.module.module"]
        .sudo()
        .search([], order="id")
        .read(["name", "state", "latest_version"])
    )


def _modules(env):
    return {
        row.name: row
        for row in env["ir.module.module"]
        .sudo()
        .search([("name", "in", list(OWNERS) + list(VERSIONS) + [SUITE])])
    }


def _rows(env):
    rows = (
        env["ir.model.data"]
        .sudo()
        .search([("module", "in", list(OWNERS))], order="module,name")
    )
    declared = catalog()["xmlids"]
    actual = {name: {} for name in OWNERS}
    for row in rows:
        actual[row.module][row.name] = row.model
    _require(actual == declared, "legacy XML-id catalog differs; reconcile first")
    for model in set(rows.mapped("model")):
        selected = rows.filtered(lambda row: row.model == model)
        _require(model in env, "metadata target model is unavailable: " + model)
        existing = set(env[model].sudo().browse(selected.mapped("res_id")).exists().ids)
        _require(
            set(selected.mapped("res_id")) <= existing,
            "dangling legacy XML-id target in " + model,
        )
    return rows


def _canonical(env, row):
    data = (
        env["ir.model.data"]
        .sudo()
        .search([("module", "=", OWNERS[row.module]), ("name", "=", row.name)])
    )
    _require(len(data) <= 1, "duplicate canonical XML-id")
    if data:
        _require(
            (data.model, data.res_id) == (row.model, row.res_id),
            "canonical XML-id collision: " + row.module + "." + row.name,
        )
    return data


def _dependencies(env, modules):
    result = (
        env["ir.module.module.dependency"]
        .sudo()
        .search(
            [("module_id", "in", [modules[name].id for name in VERSIONS])], order="id"
        )
    )
    return [
        {
            "id": row.id,
            "module_id": row.module_id.id,
            "name": row.name,
            "auto_install_required": row.auto_install_required,
        }
        for row in result
    ]


def _schema(env, module_ids):
    return {
        model: [
            {"id": row.id, "name": row.name, "module": row.module.id}
            for row in env[model]
            .sudo()
            .search([("module", "in", module_ids)], order="id")
        ]
        for model in SCHEMA_MODELS
    }


def _target_hashes(env, rows):
    """Metadata values remain private; the receipt contains only hashes."""
    result = {}
    for model in sorted(set(rows.mapped("model"))):
        records = (
            env[model]
            .sudo()
            .with_context(lang=None)
            .browse(
                sorted(
                    set(rows.filtered(lambda row: row.model == model).mapped("res_id"))
                )
            )
        )
        fields = sorted(
            name
            for name, field in records._fields.items()
            if field.store
            and field.type not in ("one2many", "binary")
            and name not in ("write_date", "write_uid", "__last_update")
        )
        values = records.read(fields)
        for record, value in zip(records, values):
            for name in fields:
                field = records._fields[name]
                if field.type == "many2one":
                    value[name] = record[name].id or False
                elif field.type == "many2many":
                    value[name] = sorted(record[name].ids)
        result[model] = hashlib.sha256(
            json.dumps(values, sort_keys=True, default=str).encode()
        ).hexdigest()
    return result


def _snapshot(env):
    modules = _modules(env)
    rows = _rows(env)
    data = []
    for row in rows:
        canonical = _canonical(env, row)
        data.append(
            {
                "id": row.id,
                "module": row.module,
                "name": row.name,
                "model": row.model,
                "res_id": row.res_id,
                "noupdate": row.noupdate,
                "canonical": canonical.read(
                    ["module", "name", "model", "res_id", "noupdate"]
                )[0]
                if canonical
                else None,
            }
        )
    return {
        "modules": _all_module_states(env),
        "metadata": data,
        "dependencies": _dependencies(env, modules),
        "schema": _schema(env, [row.id for row in modules.values()]),
        "target_hashes": _target_hashes(env, rows),
    }


def _validate_original(env):
    modules = _modules(env)
    _require(
        not env["ir.module.module"].sudo().search_count([("state", "in", TRANSIENT)]),
        "transient module states must be resolved by their owning operation",
    )
    _require(not any(markers(env).values()), "fusion data has already committed")
    for name, version in dict(LEGACY_VERSIONS, **PREDECESSOR_VERSIONS).items():
        row = modules.get(name)
        _require(
            row and row.state == "installed" and row.latest_version == version,
            "unsupported predecessor: " + name + "; use the documented prior release",
        )
    suite = modules.get(SUITE)
    _require(not suite or suite.state == "uninstalled", "unexpected Suite state")
    rows = _rows(env)
    for row in rows:
        canonical = _canonical(env, row)
        if row.module in ("marketing_center_crm", "marketing_center_dashboard"):
            _require(bool(canonical), "previous Base adoption is incomplete")
    old_ids = [modules[name].id for name in OWNERS]
    for model, records in _schema(env, old_ids).items():
        actual = {name: [] for name in OWNERS}
        names = {modules[name].id: name for name in OWNERS}
        for record in records:
            actual[names[record["module"]]].append(record["name"])
        expected = {name: [] for name in OWNERS}
        expected.update(catalog()["schema"][model])
        _require(
            {key: sorted(value) for key, value in actual.items()} == expected,
            "schema ownership catalog differs: " + model,
        )
        for record in records:
            owner_id = modules[OWNERS[names[record["module"]]]].id
            _require(
                not env[model]
                .sudo()
                .search_count(
                    [("module", "=", owner_id), ("name", "=", record["name"])]
                ),
                "schema owner collision: " + record["name"],
            )
    dependencies = env["ir.module.module.dependency"].sudo()
    consumers = dependencies.search(
        [("name", "in", list(OWNERS)), ("module_id.state", "=", "installed")]
    )
    expected_consumer = (
        "marketing_center_website_whatsapp",
        "marketing_center_website_crm",
    )
    # Legacy packages themselves cease to be runtime consumers after retirement.
    consumers = consumers.filtered(lambda row: row.module_id.name not in OWNERS)
    _require(
        len(consumers) == 1
        and (consumers.module_id.name, consumers.name) == expected_consumer,
        "unexpected installed legacy consumer",
    )
    installed = set(
        env["ir.module.module"]
        .sudo()
        .search([("state", "=", "installed")])
        .mapped("name")
    )
    for name, needed in DEPENDENCIES.items():
        current = modules[name].dependencies_id
        _require(
            len(current.mapped("name")) == len(set(current.mapped("name"))),
            "duplicate dependency",
        )
        translated = {OWNERS.get(dep.name, dep.name) for dep in current}
        _require(
            translated <= set(needed), "preparation would delete an original dependency"
        )
        _require(set(needed) <= installed, "a prerequisite is not already installed")
        _require(
            not any(dep.auto_install_required for dep in current),
            "unexpected owner auto-install requirements",
        )
    return modules, rows, consumers


def prepare(env):
    """Validate everything before mutations; return an uncommitted receipt."""
    modules, rows, consumer = _validate_original(env)
    original = _snapshot(env)
    created_aliases, created_dependencies = [], []
    data = env["ir.model.data"].sudo()
    for row in rows:
        if not _canonical(env, row):
            created_aliases.append(
                data.create(
                    {
                        "module": OWNERS[row.module],
                        "name": row.name,
                        "model": row.model,
                        "res_id": row.res_id,
                        "noupdate": row.noupdate,
                    }
                ).id
            )
    rows.write({"noupdate": True})
    for legacy, owner in OWNERS.items():
        for model in SCHEMA_MODELS:
            env[model].sudo().search([("module", "=", modules[legacy].id)]).write(
                {"module": modules[owner].id}
            )
    consumer.write({"name": "marketing_center_website"})
    for name, needed in DEPENDENCIES.items():
        existing = set(modules[name].dependencies_id.mapped("name"))
        for dependency in sorted(set(needed) - existing):
            created_dependencies.append(
                env["ir.module.module.dependency"]
                .sudo()
                .create(
                    {
                        "module_id": modules[name].id,
                        "name": dependency,
                        "auto_install_required": False,
                    }
                )
                .id
            )
    retired = [modules[name].id for name in OWNERS]
    if modules.get(SUITE):
        retired.append(modules[SUITE].id)
    env["ir.module.module"].sudo().browse(retired).write({"state": "uninstallable"})
    env.flush_all()
    assert_prepared(env)
    return {
        "format": 1,
        "original": original,
        "prepared": _snapshot(env),
        "created_aliases": created_aliases,
        "created_dependencies": created_dependencies,
    }


def assert_prepared(env):
    """Read-only source guard, also usable before merged models are initialized."""
    modules = _modules(env)
    legacy = [modules[name] for name in OWNERS if name in modules]
    if not legacy:
        for name, version in VERSIONS.items():
            row = modules.get(name)
            _require(
                not row
                or not row.latest_version
                or row.latest_version in (version,) + SUCCESSOR_VERSIONS.get(name, ()),
                "existing owner without bridges is unsupported: " + name,
            )
        _require(
            not env["ir.model.data"]
            .sudo()
            .search_count([("module", "in", list(OWNERS))]),
            "orphan legacy metadata",
        )
        return
    _require(len(legacy) == len(OWNERS), "partial predecessor graph is unsupported")
    for name, version in LEGACY_VERSIONS.items():
        row = modules[name]
        _require(
            row.state == "uninstallable" and row.latest_version == version,
            "prepare legacy ownership before upgrading: " + name,
        )
    suite = modules.get(SUITE)
    _require(not suite or suite.state == "uninstallable", "retire obsolete Suite first")
    for name, version in PREDECESSOR_VERSIONS.items():
        row = modules.get(name)
        _require(
            row
            and row.latest_version
            in (version, VERSIONS[name]) + SUCCESSOR_VERSIONS.get(name, ()),
            "unsupported retained owner lineage: " + name,
        )
    rows = _rows(env)
    for row in rows:
        _require(
            row.noupdate and bool(_canonical(env, row)), "incomplete canonical adoption"
        )
    for model in SCHEMA_MODELS:
        _require(
            not env[model]
            .sudo()
            .search_count([("module", "in", [r.id for r in legacy])]),
            "schema still owned by retired addon",
        )
    _require(
        not env["ir.module.module.dependency"]
        .sudo()
        .search_count(
            [
                ("name", "in", list(OWNERS)),
                ("module_id.state", "in", ("installed", "to upgrade")),
            ]
        ),
        "active dependency still targets a retired addon",
    )
    for name, needed in DEPENDENCIES.items():
        dependencies = modules[name].dependencies_id
        if name == "marketing_center_website" and "web" in dependencies.mapped("name"):
            ingress = (
                env["ir.module.module"]
                .sudo()
                .search([("name", "=", "marketing_center_web_ingress")])
            )
            _require(
                ingress and ingress.state == "uninstallable",
                "ingress adoption is incomplete",
            )
            aliases = (
                env["ir.model.data"]
                .sudo()
                .search([("module", "=", "marketing_center_web_ingress")])
            )
            for alias in aliases:
                canonical = (
                    env["ir.model.data"]
                    .sudo()
                    .search([("module", "=", name), ("name", "=", alias.name)])
                )
                _require(
                    alias.noupdate
                    and len(canonical) == 1
                    and (canonical.model, canonical.res_id)
                    == (alias.model, alias.res_id),
                    "ingress adoption is incomplete",
                )
            needed = tuple(
                "web" if dep == "marketing_center_web_ingress" else dep
                for dep in needed
            )
        allowed = [set(needed)]
        if name == "marketing_center_website_whatsapp":
            # update_list may refresh dependencies before the upgrade migrates
            # persisted versions. Accept only the exact historical or P2 graph.
            allowed.append(
                set(needed) | {"marketing_center_contact_center", "queue_job"}
            )
        current = dependencies.mapped("name")
        _require(
            set(current) in allowed
            and len(current) == len(set(current))
            and not any(dependencies.mapped("auto_install_required")),
            "prepared manifest dependencies differ: " + name,
        )


def inverse(env, receipt):
    """Restore recorded metadata only before any data-stage commit/flag witness."""
    _require(receipt.get("format") == 1, "unsupported receipt format")
    _require(not any(markers(env).values()), "committed fusion data forbids inverse")
    _require(
        not env["ir.module.module"].sudo().search_count([("state", "in", TRANSIENT)]),
        "pending module flags forbid inverse",
    )
    _require(
        _snapshot(env) == receipt["prepared"],
        "prepared projection changed; inverse refused",
    )
    original = receipt["original"]
    data = env["ir.model.data"].sudo()
    data.browse(receipt["created_aliases"]).unlink()
    for row in original["metadata"]:
        data.browse(row["id"]).write({"noupdate": row["noupdate"]})
    for model, records in original["schema"].items():
        for record in records:
            target = env[model].sudo().browse(record["id"])
            if target.module.id != record["module"]:
                target.write({"module": record["module"]})
    dependencies = env["ir.module.module.dependency"].sudo()
    dependencies.browse(receipt["created_dependencies"]).unlink()
    for row in original["dependencies"]:
        dependencies.browse(row["id"]).write(
            {key: value for key, value in row.items() if key != "id"}
        )
    for row in original["modules"]:
        if row["name"] in OWNERS or row["name"] == SUITE:
            env["ir.module.module"].sudo().browse(row["id"]).write(
                {"state": row["state"]}
            )
    env.flush_all()
    _require(_snapshot(env) == original, "inverse did not restore original metadata")


def remove_owner_aliases(env, owner):
    """Native owner uninstall must not leave retired aliases retaining its data."""
    legacy = [name for name, target in OWNERS.items() if target == owner]
    data = env["ir.model.data"].sudo()
    rows = data.search([("module", "in", legacy)])
    for row in rows:
        _require(
            bool(_canonical(env, row)), "uninstall alias lacks its canonical owner"
        )
    rows.unlink()
    # Actual owner uninstall also retires per-website copies in its old namespace.
    # This native cleanup is deliberately never part of preparation or upgrade.
    env["ir.module.module"].sudo().search(
        [("name", "in", legacy)]
    )._remove_copied_views()
