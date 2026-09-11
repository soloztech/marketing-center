/** @odoo-module **/

import {Component, onWillDestroy, useChildSubEnv, useState} from "@odoo/owl";
import {CatalogBrowser} from "@marketing_center_catalog/js/catalog_browser";
import {Dialog} from "@web/core/dialog/dialog";
import {_t} from "@web/core/l10n/translation";

export class CatalogDialog extends Component {
    setup() {
        this.state = useState({selected: {}, busy: false, error: ""});
        const dialogData = useState(this.env.dialogData);
        useChildSubEnv({
            dialogData: {
                get id() {
                    return dialogData.id;
                },
                get isActive() {
                    return dialogData.isActive;
                },
                scrollToOrigin: dialogData.scrollToOrigin,
                close: () => this.closeWhenIdle(),
            },
        });
        this.alive = true;
        onWillDestroy(() => {
            this.alive = false;
        });
    }

    closeWhenIdle() {
        if (!this.state.busy) {
            this.props.close();
        }
    }

    get title() {
        return _t("Content Catalog");
    }

    get selectedIds() {
        return Object.keys(this.state.selected)
            .filter((id) => this.state.selected[id])
            .map(Number);
    }

    get selectionCount() {
        return this.selectedIds.length;
    }

    loadSubjects(filters) {
        return this.props.store.call(
            "search_catalog_subjects",
            [this.props.channelId],
            filters
        );
    }

    loadItems(subjectId, filters) {
        return this.props.store.call("search_catalog_content", [this.props.channelId], {
            ...filters,
            subject_id: subjectId,
        });
    }

    searchProducts(query) {
        return this.props.store.call("search_catalog_products", [
            this.props.channelId,
            query,
        ]);
    }

    onSelectionChange(items) {
        if (!this.state.busy) {
            this.state.selected = {...items};
        }
    }

    async addToMessage() {
        if (this.state.busy || !this.selectionCount || !this.props.canAdd()) {
            return;
        }
        this.state.busy = true;
        this.state.error = "";
        try {
            const payload = await this.props.store.call("prepare_catalog_content", [
                this.props.channelId,
                this.selectedIds,
            ]);
            if (!this.alive || !this.props.canAdd()) {
                if (this.alive) {
                    this.state.error = _t(
                        "The conversation or draft changed. Reopen the catalog to add content."
                    );
                }
                return;
            }
            const inserted = await this.props.onAdd(payload, () => this.alive);
            if (!this.alive) {
                return;
            }
            const insertedIds = new Set(inserted);
            this.state.selected = Object.fromEntries(
                Object.entries(this.state.selected).filter(
                    ([id]) => !insertedIds.has(Number(id))
                )
            );
            if (this.selectionCount) {
                this.state.error = _t(
                    "Some content was not added. The prepared items remain in the draft; check the remaining files before retrying."
                );
            } else {
                this.props.close();
            }
        } catch (_error) {
            if (this.alive) {
                this.state.error = _t(
                    "Could not add the selected content. Check its availability, the draft length and your access."
                );
            }
        } finally {
            if (this.alive) {
                this.state.busy = false;
            }
        }
    }
}

CatalogDialog.template = "marketing_center_catalog_contact_center.CatalogDialog";
CatalogDialog.components = {Dialog, CatalogBrowser};
CatalogDialog.props = {
    close: Function,
    store: Object,
    channelId: Number,
    canAdd: Function,
    onAdd: Function,
};
