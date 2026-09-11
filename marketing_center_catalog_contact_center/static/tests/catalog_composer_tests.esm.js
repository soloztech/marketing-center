/** @odoo-module **/
/* global QUnit */

import {
    addCatalogItems,
    appendCatalogText,
    catalogContextCurrent,
} from "@marketing_center_catalog_contact_center/js/message_composer.esm";
import {
    click,
    getFixture,
    makeDeferred,
    mount,
    nextTick,
    triggerHotkey,
} from "@web/../tests/helpers/utils";

import {CatalogDialog} from "@marketing_center_catalog_contact_center/js/catalog_dialog.esm";
import {hotkeyService} from "@web/core/hotkeys/hotkey_service";
import {makeFakeDialogService} from "@web/../tests/helpers/mock_services";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {registry} from "@web/core/registry";
import {uiService} from "@web/core/ui/ui_service";

QUnit.module("Content Catalog composer bridge");

function fixture() {
    const context = {source: {channelId: 17, customerKey: "11:12"}, generation: 4};
    const sources = [];
    const composer = {
        local: {body: "Existing draft"},
        activeChannelId: 17,
        bodyMaxLength: 65536,
        draftSourceCurrent: (source, generation) =>
            source.channelId === 17 && generation === 4,
        persistDraft: () => true,
        resize: () => true,
        store: {
            addDraftAttachment: async (source) => {
                sources.push(source);
                return true;
            },
        },
    };
    return {composer, context, sources};
}

function file(id) {
    return {
        id,
        text: "",
        file: {
            url: `/contact_center/catalog/17/item/${id}/file`,
            name: `${id}.pdf`,
            mimetype: "application/pdf",
        },
    };
}

QUnit.test(
    "text appends without replacing existing text and respects draft capacity",
    (assert) => {
        assert.strictEqual(
            appendCatalogText("Existing", ["FAQ", "Link"], 100),
            "Existing\n\nFAQ\n\nLink"
        );
        assert.throws(() => appendCatalogText("Existing", ["Too long"], 10));
    }
);

QUnit.test(
    "multiple files use the existing draft upload contract and text is appended",
    async (assert) => {
        const {composer, context, sources} = fixture();
        const inserted = await addCatalogItems(composer, context, {
            channel_id: 17,
            items: [{id: 1, text: "Answer", file: false}, file(2), file(3)],
        });
        assert.deepEqual(inserted, [1, 2, 3]);
        assert.strictEqual(composer.local.body, "Existing draft\n\nAnswer");
        assert.strictEqual(sources.length, 2);
        assert.strictEqual(sources[0].channelId, 17);
        assert.strictEqual(sources[0].customerKey, "11:12");
        assert.strictEqual(sources[1].url, "/contact_center/catalog/17/item/3/file");
    }
);

QUnit.test("changed conversation does not receive content", async (assert) => {
    const {composer, context, sources} = fixture();
    context.generation = 5;
    assert.notOk(catalogContextCurrent(composer, context));
    const inserted = await addCatalogItems(composer, context, {
        channel_id: 17,
        items: [{id: 1, text: "Answer", file: false}, file(2)],
    });
    assert.deepEqual(inserted, []);
    assert.strictEqual(composer.local.body, "Existing draft");
    assert.deepEqual(sources, []);
});

QUnit.test(
    "partial upload failure returns only successful items for safe retries",
    async (assert) => {
        const {composer, context} = fixture();
        let calls = 0;
        composer.store.addDraftAttachment = async () => ++calls === 1;
        const inserted = await addCatalogItems(composer, context, {
            channel_id: 17,
            items: [{id: 1, text: "Answer", file: false}, file(2), file(3), file(4)],
        });
        assert.deepEqual(inserted, [1, 2]);
        assert.strictEqual(calls, 2);
        assert.strictEqual(composer.local.body, "Existing draft\n\nAnswer");
    }
);

QUnit.test(
    "a conversation change after the first upload stops the rest",
    async (assert) => {
        const {composer, context} = fixture();
        let calls = 0;
        composer.store.addDraftAttachment = async () => {
            calls++;
            context.generation++;
            return true;
        };
        const inserted = await addCatalogItems(composer, context, {
            channel_id: 17,
            items: [file(1), file(2)],
        });
        assert.deepEqual(inserted, [1]);
        assert.strictEqual(calls, 1);
    }
);

QUnit.test("internal notes and scheduled drafts remain consultation only", (assert) => {
    const {composer, context} = fixture();
    composer.noteMode = true;
    assert.notOk(catalogContextCurrent(composer, context));
    composer.noteMode = false;
    composer.scheduleMode = true;
    assert.notOk(catalogContextCurrent(composer, context));
});

QUnit.test(
    "native error cards are handed to the composer without duplicate catalog retries",
    async (assert) => {
        const {composer, context} = fixture();
        composer.attachments = [{id: "existing", phase: "ready"}];
        let calls = 0;
        composer.store.addDraftAttachment = async () => {
            calls++;
            composer.attachments.push({id: "new-upload", phase: "error"});
            return false;
        };
        const inserted = await addCatalogItems(composer, context, {
            channel_id: 17,
            items: [file(1), file(2)],
        });
        assert.deepEqual(
            inserted,
            [1],
            "failed native card is not selected again in the catalog"
        );
        assert.strictEqual(
            calls,
            1,
            "batch pauses for the operator to inspect native upload error"
        );
        assert.strictEqual(
            composer.attachments.length,
            2,
            "existing and retryable native cards remain"
        );
    }
);

QUnit.test("closing the owner stops the file sequence", async (assert) => {
    const {composer, context} = fixture();
    let alive = true;
    let calls = 0;
    composer.store.addDraftAttachment = async () => {
        calls++;
        alive = false;
        return true;
    };
    await addCatalogItems(
        composer,
        context,
        {channel_id: 17, items: [file(1), file(2)]},
        () => alive
    );
    assert.strictEqual(calls, 1);
});

QUnit.module("Content Catalog composer bridge dialog", (hooks) => {
    hooks.beforeEach(() => {
        const services = registry.category("services");
        services.add("ui", uiService);
        services.add("hotkey", hotkeyService);
        services.add("dialog", makeFakeDialogService());
    });

    QUnit.test(
        "shared browser keeps tab selections and blocks X and Escape while adding",
        async (assert) => {
            const target = getFixture();
            const pending = makeDeferred();
            const env = await makeTestEnv();
            let closed = 0;
            const close = () => closed++;
            env.dialogData = {id: 1, isActive: true, close, scrollToOrigin: () => true};
            const subject = {
                id: 101,
                name: "Company",
                kind: "company",
                summary_text: "Company information",
                image_url: false,
                counts: {information: 0, materials: 1, faq: 2},
            };
            const item = {
                id: 1,
                name: "Warranty FAQ",
                kind: "faq",
                subject_id: 101,
                subject: "Company",
                scope: [],
                text: "Answer",
                question: "Warranty?",
                answer_html: "<p>Answer</p>",
                shareable: true,
                preview_url: false,
                preview_kind: "none",
            };
            const material = {
                id: 3,
                name: "Catalog PDF",
                kind: "file",
                subject_id: 101,
                subject: "Company",
                scope: [],
                text: "Catalog PDF",
                shareable: true,
                preview_kind: "file",
                preview_url: false,
                download_url: "/contact_center/catalog/17/item/3/download",
            };
            const store = {
                call: async (method, args, filters = {}) => {
                    assert.strictEqual(
                        args[0],
                        17,
                        "every bridge request keeps the selected channel"
                    );
                    if (method === "search_catalog_subjects") {
                        return {channel_id: 17, subjects: [subject], has_more: false};
                    }
                    if (method === "search_catalog_content") {
                        assert.strictEqual(filters.subject_id, 101);
                        return {
                            channel_id: 17,
                            subject,
                            items:
                                filters.section === "faq"
                                    ? [item, {...item, id: 2, shareable: false}]
                                    : filters.section === "materials"
                                    ? [material]
                                    : [],
                            has_more: false,
                        };
                    }
                    if (method === "search_catalog_products") {
                        return [];
                    }
                    return pending;
                },
            };
            const dialog = await mount(CatalogDialog, target, {
                env,
                props: {
                    close,
                    store,
                    channelId: 17,
                    canAdd: () => true,
                    onAdd: async () => [1, 3],
                },
            });
            assert.containsOnce(target, ".o_cc_catalog");
            await click(
                target,
                ".o_catalog_subject_card[data-subject-id='101'] button.o_catalog_subject_open"
            );
            await click(target, "button[data-section='faq']");
            const selectors = ".o_catalog_item_select input[type='checkbox']";
            assert.strictEqual(target.querySelectorAll(selectors)[1].disabled, true);
            await click(target.querySelector(selectors));
            await click(target, "button[data-section='materials']");
            await click(target.querySelector(selectors));
            assert.deepEqual(
                dialog.selectedIds,
                [1, 3],
                "selections persist across tabs"
            );
            await click(target.querySelector("footer .btn-primary"));
            assert.ok(dialog.state.busy);
            await click(target.querySelector("header .btn-close"));
            triggerHotkey("escape");
            await nextTick();
            assert.strictEqual(
                closed,
                0,
                "X and Escape cannot hide the active preparation"
            );
            pending.resolve({
                channel_id: 17,
                items: [{id: 1, text: "Answer", file: false}, file(3)],
            });
            await nextTick();
            assert.strictEqual(
                closed,
                1,
                "dialog closes after the selection reaches the draft"
            );
        }
    );
});

QUnit.test(
    "partial handoff updates the controlled selection without reselecting prepared items",
    async (assert) => {
        const selected = {1: {id: 1}, 2: {id: 2}, 3: {id: 3}};
        const wrapper = {
            alive: true,
            state: {selected, busy: false, error: ""},
            get selectionCount() {
                return Object.keys(this.state.selected).length;
            },
            get selectedIds() {
                return Object.keys(this.state.selected).map(Number);
            },
            props: {
                channelId: 17,
                canAdd: () => true,
                store: {call: async () => ({channel_id: 17, items: []})},
                onAdd: async () => [1, 2],
                close: () => assert.ok(false, "remaining selection keeps popup open"),
            },
        };
        await CatalogDialog.prototype.addToMessage.call(wrapper);
        assert.deepEqual(wrapper.selectedIds, [3]);
        assert.notStrictEqual(
            wrapper.state.selected,
            selected,
            "new map updates the controlled browser props"
        );
        assert.notOk(wrapper.state.busy);
    }
);
