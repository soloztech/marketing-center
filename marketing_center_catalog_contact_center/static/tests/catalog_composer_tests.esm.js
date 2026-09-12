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
} from "@web/../tests/helpers/utils";

import {CatalogPanel} from "@marketing_center_catalog_contact_center/js/catalog_dialog.esm";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
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

QUnit.module("Content Catalog side panel", (hooks) => {
    hooks.beforeEach(() => {
        const services = registry.category("services");
        services.add("ui", uiService);
        services.add("hotkey", hotkeyService);
        services.add("dialog", makeFakeDialogService());
    });

    QUnit.test(
        "shared browser keeps tab selections and prepares the draft in the sidebar",
        async (assert) => {
            const target = getFixture();
            const pending = makeDeferred();
            const env = await makeTestEnv();
            let closed = 0;
            const close = () => closed++;
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
            const panel = await mount(CatalogPanel, target, {
                env,
                props: {
                    close,
                    store,
                    channelId: 17,
                    getDraft: () => ({canAdd: () => true, onAdd: async () => [1, 3]}),
                },
            });
            assert.containsOnce(target, "aside.o_cc_catalog_panel");
            assert.containsNone(target, ".modal");
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
                panel.selectedIds,
                [1, 3],
                "selections persist across tabs"
            );
            await click(target.querySelector("footer .cc-primary-button"));
            assert.ok(panel.state.busy);
            assert.ok(target.querySelector("footer .cc-primary-button").disabled);
            assert.strictEqual(closed, 0, "preparation stays visible until ready");
            pending.resolve({
                channel_id: 17,
                items: [{id: 1, text: "Answer", file: false}, file(3)],
            });
            await nextTick();
            assert.strictEqual(
                closed,
                1,
                "panel closes after the selection reaches the draft"
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
                getDraft: () => ({canAdd: () => true, onAdd: async () => [1, 2]}),
                store: {call: async () => ({channel_id: 17, items: []})},
                close: () => assert.ok(false, "remaining selection keeps popup open"),
            },
        };
        await CatalogPanel.prototype.addToMessage.call(wrapper);
        assert.deepEqual(wrapper.selectedIds, [3]);
        assert.notStrictEqual(
            wrapper.state.selected,
            selected,
            "new map updates the controlled browser props"
        );
        assert.notOk(wrapper.state.busy);
    }
);

QUnit.test(
    "catalog switches exclusively with other panels and respects access",
    (assert) => {
        const app = Object.create(ContactCenterApp.prototype);
        app.ui = {sidePanel: "contact"};
        app.store = {
            capabilities: {content_catalog: true},
            state: {detailsOpen: true},
            selectedConversation: {channel_id: 17},
            toggleDetails() {
                this.state.detailsOpen = !this.state.detailsOpen;
            },
        };
        app.toggleCatalogPanel();
        assert.ok(app.catalogPanelSelected);
        assert.notOk(app.contactPanelSelected);
        assert.ok(app.store.state.detailsOpen);
        app.toggleSidePanel("crm");
        assert.notOk(app.catalogPanelSelected);
        app.toggleCatalogPanel();
        assert.ok(app.catalogPanelSelected);
        app.toggleContactPanel();
        assert.ok(app.contactPanelSelected);
        assert.notOk(app.catalogPanelSelected);
        assert.ok(app.store.state.detailsOpen);
        app.toggleCatalogPanel();
        app.toggleCatalogPanel();
        assert.notOk(app.store.state.detailsOpen, "second click closes the catalog");
        app.toggleCatalogPanel();
        assert.ok(app.store.state.detailsOpen, "reopening selects the same panel");
        app.store.capabilities.content_catalog = false;
        assert.notOk(app.canViewCatalog);
        assert.notOk(app.catalogPanelSelected);
        app.toggleContactPanel();
        app.toggleCatalogPanel();
        assert.ok(app.contactPanelSelected, "missing capability cannot reopen catalog");
    }
);

QUnit.test(
    "catalog draft scope stops when its composer is replaced",
    async (assert) => {
        const {composer, sources} = fixture();
        composer.draftSourceContext = () => ({channelId: 17, customerKey: "11:12"});
        composer.uploadGeneration = 4;
        const app = Object.create(ContactCenterApp.prototype);
        app.catalogComposer = composer;
        app.ui = {sidePanel: "catalog"};
        app.store = {
            capabilities: {content_catalog: true},
            state: {detailsOpen: true},
            selectedConversation: {channel_id: 17},
        };
        const draft = app.getCatalogDraft();
        assert.ok(draft.canAdd());
        app.store.state.detailsOpen = false;
        assert.notOk(
            draft.canAdd(),
            "closing invalidates pending preparation before unmount"
        );
        app.store.state.detailsOpen = true;
        app.catalogComposer = null;
        assert.notOk(draft.canAdd());
        assert.deepEqual(
            await draft.onAdd({channel_id: 17, items: [file(2)]}, () => true),
            []
        );
        assert.deepEqual(sources, [], "detached composer receives no file");
        assert.strictEqual(
            app.getCatalogDraft(),
            null,
            "read-only chat remains consultation only"
        );
    }
);

QUnit.test(
    "closing or switching conversations cancels pending catalog handoff",
    async (assert) => {
        const target = getFixture();
        const env = await makeTestEnv();
        const pending = makeDeferred();
        let added = 0;
        let panel;
        const calls = [];
        const store = {
            call: async (method, args) => {
                calls.push([method, args[0]]);
                return method === "prepare_catalog_content"
                    ? pending
                    : {subjects: [], has_more: false};
            },
        };
        const mountPanel = (channelId) =>
            mount(CatalogPanel, target, {
                env,
                props: {
                    channelId,
                    store,
                    close: () => panel.__owl__.app.destroy(),
                    getDraft: () => ({
                        canAdd: () => true,
                        onAdd: async () => {
                            added++;
                            return [1];
                        },
                    }),
                },
            });
        panel = await mountPanel(17);
        panel.state.selected = {1: {id: 1}};
        await nextTick();
        const adding = panel.addToMessage();
        await nextTick();
        await click(target, "header button");
        pending.resolve({channel_id: 17, items: [file(1)]});
        await adding;
        assert.strictEqual(added, 0, "closed panel never hands content to the draft");
        assert.containsNone(target, ".o_cc_catalog_panel");
        panel = await mountPanel(18);
        assert.deepEqual(
            panel.selectedIds,
            [],
            "new conversation has no stale selection"
        );
        assert.deepEqual(calls, [
            ["search_catalog_subjects", 17],
            ["prepare_catalog_content", 17],
            ["search_catalog_subjects", 18],
        ]);
    }
);
