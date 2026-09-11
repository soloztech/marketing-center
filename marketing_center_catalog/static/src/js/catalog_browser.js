/** @odoo-module **/

import {Component, markup, onWillDestroy, onWillStart, useState} from "@odoo/owl";
import {_t} from "@web/core/l10n/translation";
import {CatalogMediaPreview} from "./catalog_media_preview";

export class CatalogBrowser extends Component {
    setup() {
        this.state = useState({
            subjects: [],
            items: [],
            subject: null,
            item: null,
            section: "information",
            query: "",
            productQuery: "",
            productResults: [],
            productsOpen: false,
            products: (this.props.initialProductIds || []).map((id) => ({
                id,
                name:
                    (
                        (this.props.initialProductLabels || []).find(
                            (p) => p.id === id
                        ) || {}
                    ).name || `#${id}`,
            })),
            loading: false,
            error: "",
            hasMore: false,
        });
        this.requestId = 0;
        this.productRequestId = 0;
        this.alive = true;
        onWillStart(() => this.search());
        onWillDestroy(() => {
            this.alive = false;
            this.requestId++;
            this.productRequestId++;
        });
    }

    get sections() {
        return [
            {id: "information", name: _t("Information")},
            {id: "materials", name: _t("Materials")},
            {id: "faq", name: _t("FAQ")},
        ];
    }

    applicability(item) {
        return Array.isArray(item.applicability)
            ? item.applicability.join(", ")
            : item.applicability;
    }

    html(value) {
        return markup(value || "");
    }

    async search(append = false) {
        const requestId = ++this.requestId;
        const subject = this.state.subject;
        this.state.item = null;
        this.state.loading = true;
        this.state.error = "";
        if (!append) {
            this.appliedFilters = {
                query: this.state.query,
                product_ids: this.state.products.map((p) => p.id),
                limit: 24,
            };
        }
        const filters = {
            ...this.appliedFilters,
            offset: append
                ? subject
                    ? this.state.items.length
                    : this.state.subjects.length
                : 0,
        };
        if (!append) {
            this.state.items = [];
            this.state.subjects = [];
        }
        try {
            const page = subject
                ? await this.props.loadItems(subject.id, {
                      ...filters,
                      section: this.state.section,
                  })
                : await this.props.loadSubjects(filters);
            if (!this.alive || requestId !== this.requestId) return;
            if (subject) {
                this.state.items = append
                    ? [...this.state.items, ...page.items]
                    : page.items;
                if (page.subject) this.state.subject = page.subject;
            } else {
                this.state.subjects = append
                    ? [...this.state.subjects, ...page.subjects]
                    : page.subjects;
            }
            this.state.hasMore = page.has_more;
        } catch (_error) {
            if (this.alive && requestId === this.requestId) {
                this.state.error = _t(
                    "Could not load the catalog. Try again or check your access."
                );
            }
        } finally {
            if (this.alive && requestId === this.requestId) this.state.loading = false;
        }
    }

    openSubject(subject) {
        this.state.subject = subject;
        this.state.section = "information";
        this.state.query = "";
        return this.search();
    }

    goHome() {
        this.state.subject = null;
        this.state.query = "";
        return this.search();
    }

    changeSection(section) {
        this.state.section = section;
        return this.search();
    }

    toggleItem(item) {
        if (
            !this.props.selectionEnabled ||
            this.props.selectionDisabled ||
            !item.shareable
        )
            return;
        const selected = {...this.props.selectedItems};
        if (selected[item.id]) delete selected[item.id];
        else selected[item.id] = item;
        this.props.onSelectionChange(selected);
    }

    async searchProducts() {
        const requestId = ++this.productRequestId;
        try {
            const products = await this.props.searchProducts(this.state.productQuery);
            if (this.alive && requestId === this.productRequestId) {
                this.state.productResults = products.filter(
                    (p) => !this.state.products.some((chosen) => chosen.id === p.id)
                );
            }
        } catch (_error) {
            if (this.alive && requestId === this.productRequestId) {
                this.state.error = _t(
                    "Could not load products. Try again or check your access."
                );
            }
        }
    }

    chooseProduct(product) {
        this.state.products.push(product);
        this.state.productQuery = "";
        this.state.productResults = [];
        this.productRequestId++;
        return this.goHome();
    }

    removeProduct(id) {
        this.state.products = this.state.products.filter((p) => p.id !== id);
        return this.goHome();
    }
}
CatalogBrowser.template = "marketing_center_catalog.Browser";
CatalogBrowser.components = {CatalogMediaPreview};
CatalogBrowser.props = {
    loadSubjects: Function,
    loadItems: Function,
    searchProducts: {type: Function, optional: true},
    initialProductIds: {type: Array, optional: true},
    initialProductLabels: {type: Array, optional: true},
    selectionEnabled: {type: Boolean, optional: true},
    selectionDisabled: {type: Boolean, optional: true},
    selectedItems: {type: Object, optional: true},
    onSelectionChange: {type: Function, optional: true},
};
CatalogBrowser.defaultProps = {
    selectedItems: {},
    selectionEnabled: false,
    selectionDisabled: false,
};
