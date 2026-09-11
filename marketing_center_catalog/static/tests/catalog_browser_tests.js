/** @odoo-module **/
/* global QUnit */

import {
    CatalogMediaPreview,
    CatalogMediaPreviewField,
} from "@marketing_center_catalog/js/catalog_media_preview";
import {Component, useState, xml} from "@odoo/owl";
import {
    click,
    clickSave,
    destroy,
    editInput,
    getFixture,
    makeDeferred,
    mount,
    nextTick,
    patchWithCleanup,
} from "@web/../tests/helpers/utils";
import {makeView, setupViewRegistries} from "@web/../tests/views/helpers";
import {CatalogBrowser} from "@marketing_center_catalog/js/catalog_browser";

const SUBJECTS = [
    {
        id: 101,
        name: "Modular Line",
        image_url: false,
        counts: {information: 1, materials: 1, faq: 1},
    },
    {
        id: 102,
        name: "Company",
        image_url: false,
        counts: {information: 1, materials: 0, faq: 0},
    },
];

function item(id, kind = "text", values = {}) {
    return {
        id,
        name: `Content ${id}`,
        kind,
        preview_kind: kind,
        shareable: true,
        text: "Technical information",
        ...values,
    };
}

function props(overrides = {}) {
    return {
        loadSubjects: async () => ({subjects: SUBJECTS, has_more: false}),
        loadItems: async (subjectId) => ({
            subject: SUBJECTS.find((subject) => subject.id === subjectId),
            items: [item(subjectId)],
            has_more: false,
        }),
        ...overrides,
    };
}

QUnit.module("Content Catalog browser");

QUnit.test(
    "subject cards, detail and back navigation keep product filters removable",
    async (assert) => {
        const target = getFixture();
        const filters = [];
        await mount(CatalogBrowser, target, {
            props: props({
                loadSubjects: async (filter) => {
                    filters.push(filter);
                    return {subjects: SUBJECTS, has_more: false};
                },
                loadItems: async (subjectId, filter) => {
                    assert.strictEqual(subjectId, 101);
                    assert.strictEqual(filter.section, "information");
                    return {
                        subject: SUBJECTS[0],
                        items: [item(11, "text", {text: "<script>example</script>"})],
                        has_more: false,
                    };
                },
                searchProducts: async (query) => {
                    assert.strictEqual(query, "SKU");
                    return [{id: 55, name: "[SKU] Product"}];
                },
            }),
        });
        assert.containsN(target, ".o_catalog_subject_card", 2);
        await click(
            target,
            ".o_catalog_subject_card[data-subject-id='101'] .o_catalog_subject_open"
        );
        assert.containsN(target, "button[data-section]", 3);
        await click(target, ".o_catalog_item_open");
        assert.strictEqual(
            target.querySelector(".o_catalog_detail_text").textContent.trim(),
            "<script>example</script>"
        );
        assert.containsNone(
            target,
            ".o_catalog_detail_text script",
            "plain text fallback never creates HTML nodes"
        );
        await click(target.querySelectorAll(".o_catalog_breadcrumb button")[1]);
        assert.containsOnce(target, ".o_catalog_item_grid");
        await click(target, ".o_catalog_breadcrumb button");
        assert.containsN(target, ".o_catalog_subject_card", 2);
        await click(target, ".o_catalog_product_toggle");
        await editInput(target, ".o_catalog_product_search input", "SKU");
        await click(target, ".o_catalog_product_search button[type='submit']");
        await click(target, ".o_catalog_product_search .list-group-item");
        assert.deepEqual(filters[filters.length - 1].product_ids, [55]);
        assert.containsOnce(target, ".o_catalog_product_chip");
        await click(target, ".o_catalog_product_chip button");
        assert.deepEqual(filters[filters.length - 1].product_ids, []);
        assert.containsNone(target, ".o_catalog_product_chip");
    }
);

QUnit.test(
    "a slow Information response cannot replace a newer FAQ tab",
    async (assert) => {
        const target = getFixture();
        const pending = makeDeferred();
        const browser = await mount(CatalogBrowser, target, {
            props: props({
                loadItems: async (_subjectId, filter) =>
                    filter.section === "information"
                        ? pending
                        : {
                              subject: SUBJECTS[0],
                              items: [item(22, "faq")],
                              has_more: false,
                          },
            }),
        });
        await click(
            target,
            ".o_catalog_subject_card[data-subject-id='101'] .o_catalog_subject_open"
        );
        await click(target, "button[data-section='faq']");
        assert.strictEqual(browser.state.section, "faq");
        assert.containsOnce(target, ".o_catalog_item_card[data-item-id='22']");
        pending.resolve({subject: SUBJECTS[0], items: [item(11)], has_more: true});
        await nextTick();
        assert.containsOnce(target, ".o_catalog_item_card[data-item-id='22']");
        assert.containsNone(target, ".o_catalog_item_card[data-item-id='11']");
        assert.notOk(browser.state.hasMore);
    }
);

QUnit.test(
    "controlled selections survive subjects and tabs and react to parent clearing",
    async (assert) => {
        class Parent extends Component {
            setup() {
                this.state = useState({selected: {}, disabled: false});
            }
            onSelectionChange(selected) {
                this.state.selected = selected;
            }
        }
        Parent.components = {CatalogBrowser};
        Parent.props = {browserProps: Object};
        Parent.template = xml`<CatalogBrowser loadSubjects="props.browserProps.loadSubjects" loadItems="props.browserProps.loadItems" selectionEnabled="true" selectedItems="state.selected" selectionDisabled="state.disabled" onSelectionChange.bind="onSelectionChange"/>`;
        const target = getFixture();
        const parent = await mount(Parent, target, {
            props: {
                browserProps: props({
                    loadItems: async (subjectId, filter) => ({
                        subject: SUBJECTS.find((subject) => subject.id === subjectId),
                        items:
                            filter.section === "materials"
                                ? [item(12, "file", {preview_kind: "file"})]
                                : [
                                      item(subjectId),
                                      item(subjectId + 1000, "text", {
                                          shareable: false,
                                      }),
                                  ],
                        has_more: false,
                    }),
                }),
            },
        });
        await click(
            target,
            ".o_catalog_subject_card[data-subject-id='101'] .o_catalog_subject_open"
        );
        const selection = (id) => `.o_catalog_item_select[data-item-id='${id}'] input`;
        assert.ok(
            target.querySelector(selection(1101)).disabled,
            "internal item cannot be selected"
        );
        await click(target, selection(101));
        await click(target, "button[data-section='materials']");
        await click(target, selection(12));
        await click(target, ".o_catalog_breadcrumb button");
        await click(
            target,
            ".o_catalog_subject_card[data-subject-id='102'] .o_catalog_subject_open"
        );
        await click(target, selection(102));
        assert.deepEqual(
            Object.keys(parent.state.selected).map(Number),
            [12, 101, 102]
        );
        parent.state.selected = {12: parent.state.selected[12]};
        await nextTick();
        assert.notOk(
            target.querySelector(selection(102)).checked,
            "parent-cleared prepared item becomes unselected"
        );
        await click(target, ".o_catalog_breadcrumb button");
        await click(
            target,
            ".o_catalog_subject_card[data-subject-id='101'] .o_catalog_subject_open"
        );
        await click(target, "button[data-section='materials']");
        assert.ok(
            target.querySelector(selection(12)).checked,
            "remaining selection stays selected on return"
        );
        parent.state.disabled = true;
        await nextTick();
        assert.ok(
            target.querySelector(selection(12)).disabled,
            "busy parent prevents changing selection"
        );
    }
);

QUnit.test(
    "PDF thumbnails stay lazy and release pending resources on unmount",
    async (assert) => {
        let observerCallback;
        let observed = 0;
        let disconnected = 0;
        let rendered = 0;
        let cancelled = 0;
        let destroyed = 0;
        class Observer {
            constructor(callback) {
                observerCallback = callback;
            }
            observe() {
                observed++;
            }
            disconnect() {
                disconnected++;
            }
        }
        patchWithCleanup(window, {IntersectionObserver: Observer}, {pure: true});
        patchWithCleanup(CatalogMediaPreview.prototype, {
            renderPDF() {
                rendered++;
                this.renderTask = {cancel: () => cancelled++};
                this.loadingTask = {
                    destroy: () => {
                        destroyed++;
                        return Promise.resolve();
                    },
                };
            },
        });
        const media = await mount(CatalogMediaPreview, getFixture(), {
            props: {
                type: "pdf",
                url: "/marketing_center/catalog/item/11/preview",
                name: "Datasheet",
            },
        });
        assert.strictEqual(observed, 1);
        assert.strictEqual(rendered, 0, "offscreen PDF does not start loading");
        observerCallback([{isIntersecting: true}]);
        assert.strictEqual(rendered, 1);
        destroy(media);
        assert.strictEqual(cancelled, 1);
        assert.strictEqual(destroyed, 1);
        assert.ok(disconnected >= 1);
    }
);

QUnit.test(
    "load more uses applied search filters until the new input is submitted",
    async (assert) => {
        const target = getFixture();
        const calls = [];
        const browser = await mount(CatalogBrowser, target, {
            props: props({
                loadSubjects: async (filter) => {
                    calls.push({...filter});
                    return {
                        subjects: filter.offset ? [SUBJECTS[1]] : [SUBJECTS[0]],
                        has_more: !filter.offset,
                    };
                },
            }),
        });
        await editInput(target, ".o_catalog_search input", "New search not submitted");
        await browser.search(true);
        await nextTick();
        assert.strictEqual(calls[1].query, "", "pagination keeps the applied query");
        assert.strictEqual(calls[1].offset, 1);
        assert.containsN(target, ".o_catalog_subject_card", 2);
        await browser.search();
        await nextTick();
        assert.strictEqual(calls[2].query, "New search not submitted");
        assert.strictEqual(calls[2].offset, 0);
        assert.containsOnce(target, ".o_catalog_subject_card");
    }
);

QUnit.test(
    "native media field hides stale content while dirty and refreshes after save",
    async (assert) => {
        setupViewRegistries();
        class PreviewProbe extends Component {}
        PreviewProbe.props = CatalogMediaPreview.props;
        PreviewProbe.template = xml`<div class="preview-probe" t-att-data-url="props.url || ''"/>`;
        patchWithCleanup(CatalogMediaPreviewField, {
            components: {CatalogMediaPreview: PreviewProbe},
        });
        const target = getFixture();
        let name = "original.pdf";
        let stamp = "2026-09-11 10:00:00";
        let writes = 0;
        const serverData = {
            models: {
                document: {
                    fields: {
                        file_name: {type: "char", string: "Filename"},
                        preview_kind: {
                            type: "selection",
                            string: "Preview",
                            selection: [["file", "File"]],
                        },
                        write_date: {type: "datetime", string: "Updated"},
                    },
                    records: [
                        {
                            id: 11,
                            file_name: name,
                            preview_kind: "file",
                            write_date: stamp,
                        },
                    ],
                },
            },
        };
        await makeView({
            type: "form",
            resModel: "document",
            resId: 11,
            serverData,
            arch: `<form><field name="file_name"/><field name="preview_kind" widget="catalog_media_preview" options="{'large': True}"/></form>`,
            mockRPC(_route, args) {
                if (args.model === "document" && args.method === "read") {
                    assert.ok(
                        args.args[1].includes("write_date"),
                        "widget requests its timestamp dependency"
                    );
                    return [
                        {
                            id: 11,
                            file_name: name,
                            preview_kind: "file",
                            write_date: stamp,
                        },
                    ];
                }
                if (args.model === "document" && args.method === "write") {
                    name = args.args[1].file_name;
                    stamp = "2026-09-11 11:00:00";
                    writes++;
                    return true;
                }
            },
        });
        const previewUrl = () => target.querySelector(".preview-probe").dataset.url;
        const initial = previewUrl();
        assert.ok(
            initial.startsWith("/marketing_center/catalog/item/11/preview?unique=")
        );
        await editInput(
            target,
            '.o_field_widget[name="file_name"] input',
            "replacement.pdf"
        );
        assert.strictEqual(
            previewUrl(),
            "",
            "unsaved edits do not display the previous server file"
        );
        assert.ok(target.textContent.includes("Save to preview this file."));
        await clickSave(target);
        assert.strictEqual(writes, 1);
        assert.ok(
            previewUrl().startsWith("/marketing_center/catalog/item/11/preview?unique=")
        );
        assert.notStrictEqual(
            previewUrl(),
            initial,
            "new write date invalidates the old preview URL"
        );
    }
);

QUnit.test(
    "video preview pauses and releases media on replacement and unmount",
    async (assert) => {
        const assignments = [];
        let pauses = 0;
        let loads = 0;
        patchWithCleanup(window.HTMLMediaElement.prototype, {
            set src(value) {
                assignments.push(value);
            },
            get src() {
                return assignments[assignments.length - 1] || "";
            },
            pause() {
                pauses++;
            },
            load() {
                loads++;
            },
        });
        // Avoid a media download: retain the real component's lifecycle with a bare video node.
        class VideoProbe extends CatalogMediaPreview {}
        VideoProbe.template = xml`<div t-ref="root"><video/></div>`;
        class Parent extends Component {
            setup() {
                this.state = useState({url: "first-video"});
            }
        }
        Parent.components = {VideoProbe};
        Parent.template = xml`<VideoProbe type="'video'" url="state.url" large="true"/>`;
        const parent = await mount(Parent, getFixture());
        assert.deepEqual(assignments, ["first-video"]);
        parent.state.url = "second-video";
        await nextTick();
        assert.deepEqual(assignments, ["first-video", "second-video"]);
        assert.strictEqual(pauses, 1, "replaced media is paused");
        assert.strictEqual(loads, 1, "replaced source is released");
        destroy(parent);
        assert.strictEqual(pauses, 2, "unmounted media is paused");
        assert.strictEqual(loads, 2, "unmounted source is released");
    }
);
