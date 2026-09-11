/** @odoo-module **/
/* global QUnit */

import "@marketing_center_catalog/js/catalog_x2many";
import {
    addRow,
    click,
    clickSave,
    editInput,
    getFixture,
    makeDeferred,
    patchWithCleanup,
    triggerEvent,
} from "@web/../tests/helpers/utils";
import {makeView, setupViewRegistries} from "@web/../tests/views/helpers";
import {HtmlField} from "@web_editor/js/backend/html_field";
import {registry} from "@web/core/registry";

function data() {
    return {
        models: {
            subject: {
                fields: {
                    name: {type: "char", string: "Name"},
                    item_ids: {
                        type: "one2many",
                        relation: "item",
                        relation_field: "subject_id",
                        string: "Content",
                    },
                },
                records: [{id: 1, name: "Modular line", item_ids: [10]}],
            },
            item: {
                fields: {
                    name: {type: "char", string: "Question", required: true},
                    answer: {type: "html", string: "Answer"},
                    caption: {type: "char", string: "Caption"},
                    subject_id: {
                        type: "many2one",
                        relation: "subject",
                        string: "Subject",
                    },
                },
                records: [
                    {
                        id: 10,
                        name: "What is included?",
                        answer: "<p>Structure and fasteners.</p>",
                        caption: "Product photo",
                        subject_id: 1,
                    },
                ],
            },
        },
    };
}

function arch(defaultView = "list") {
    return `<form js_class="catalog_subject_form">
        <field name="name"/>
        <field name="item_ids" widget="catalog_x2many"
            mode="tree,kanban" options="{'default_view': '${defaultView}'}">
            <tree editable="bottom">
                <field name="name"/>
                <field name="answer"/>
            </tree>
            <kanban>
                <field name="name"/>
                <field name="caption"/>
                <templates><t t-name="kanban-box">
                    <div class="oe_kanban_global_click o_test_catalog_card">
                        <strong><field name="name"/></strong>
                        <span class="o_test_caption"><field name="caption"/></span>
                    </div>
                </t></templates>
            </kanban>
        </field>
    </form>`;
}

QUnit.module("Content Catalog registration views", (hooks) => {
    let target;
    hooks.beforeEach(() => {
        target = getFixture();
        setupViewRegistries();
        registry.category("fields").add("html", HtmlField, {force: true});
    });

    QUnit.test(
        "list default can show card-only fields without another read",
        async (assert) => {
            let reads = 0;
            await makeView({
                type: "form",
                resModel: "subject",
                resId: 1,
                serverData: data(),
                arch: arch(),
                mockRPC(route, {method}) {
                    if (method === "read") reads++;
                },
            });
            const initialReads = reads;
            assert.containsOnce(target, ".o_catalog_x2many .o_list_renderer");
            assert.strictEqual(
                target
                    .querySelector(".o_catalog_view_list")
                    .getAttribute("aria-pressed"),
                "true"
            );
            await click(target, ".o_catalog_view_kanban");
            assert.containsOnce(target, ".o_test_catalog_card");
            assert.strictEqual(
                target.querySelector(".o_test_caption").textContent,
                "Product photo"
            );
            assert.strictEqual(
                reads,
                initialReads,
                "switching does not reload records"
            );
            await click(target, ".o_catalog_view_list");
            assert.containsOnce(target, ".o_catalog_x2many .o_data_row");
        }
    );

    QUnit.test("kanban default also loads list-only answer fields", async (assert) => {
        await makeView({
            type: "form",
            resModel: "subject",
            resId: 1,
            serverData: data(),
            arch: arch("kanban"),
        });
        assert.containsOnce(target, ".o_test_catalog_card");
        assert.strictEqual(
            target.querySelector(".o_catalog_view_kanban").getAttribute("aria-pressed"),
            "true"
        );
        await click(target, ".o_catalog_view_list");
        assert.strictEqual(
            target.querySelector(".o_data_row [name='answer']").textContent,
            "Structure and fasteners."
        );
    });

    QUnit.test(
        "pending answer and a new inline record survive both renderers",
        async (assert) => {
            let writes = 0;
            const editorReady = makeDeferred();
            patchWithCleanup(HtmlField.prototype, {
                async startWysiwyg() {
                    await this._super(...arguments);
                    editorReady.resolve(this.wysiwyg.odooEditor.editable);
                },
            });
            const form = await makeView({
                type: "form",
                resModel: "subject",
                resId: 1,
                serverData: data(),
                arch: arch(),
                mockRPC(route, {method}) {
                    if (["write", "create"].includes(method)) writes++;
                },
            });
            await addRow(target);
            const answer = await editorReady;
            await editInput(
                target,
                ".o_selected_row [name='name'] input",
                "How to install?"
            );
            answer.innerHTML = "<p>Use the installation manual.</p>";
            await triggerEvent(answer, null, "input");
            const virtualId = form.model.root.data.item_ids.records[1].id;
            await click(target, ".o_catalog_view_kanban");
            assert.containsN(target, ".o_test_catalog_card", 2);
            assert.strictEqual(writes, 0, "presentation does not save content");
            await click(target, ".o_catalog_view_list");
            assert.strictEqual(form.model.root.data.item_ids.records[1].id, virtualId);
            assert.strictEqual(
                target.querySelectorAll(".o_data_row [name='answer']")[1].textContent,
                "Use the installation manual."
            );
            await clickSave(target);
            assert.strictEqual(
                writes,
                1,
                "the parent Save persists the new answer once"
            );
        }
    );

    QUnit.test(
        "switching preserves an incomplete new row instead of discarding it",
        async (assert) => {
            const form = await makeView({
                type: "form",
                resModel: "subject",
                resId: 1,
                serverData: data(),
                arch: arch(),
            });
            await addRow(target);
            const virtualId = form.model.root.data.item_ids.records[1].id;
            await click(target, ".o_catalog_view_kanban");
            assert.containsOnce(target, ".o_catalog_x2many .o_list_renderer");
            assert.containsOnce(target, ".o_selected_row");
            assert.strictEqual(form.model.root.data.item_ids.records[1].id, virtualId);
            await editInput(
                target,
                ".o_selected_row [name='name'] input",
                "Now complete"
            );
            await click(target, ".o_catalog_view_kanban");
            assert.containsN(target, ".o_test_catalog_card", 2);
        }
    );
});
