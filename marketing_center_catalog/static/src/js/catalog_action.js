/** @odoo-module **/

import {Component, onWillStart} from "@odoo/owl";
import {Dialog} from "@web/core/dialog/dialog";
import {useService} from "@web/core/utils/hooks";
import {registry} from "@web/core/registry";
import {_t} from "@web/core/l10n/translation";
import {CatalogBrowser} from "./catalog_browser";

export class CatalogBrowseDialog extends Component {
    setup() {
        this.orm = useService("orm");
        this.products = [];
        onWillStart(async () => {
            if (this.props.productIds.length) {
                const products = await this.orm.read(
                    "product.product",
                    this.props.productIds,
                    ["display_name"]
                );
                this.products = products.map((p) => ({id: p.id, name: p.display_name}));
            }
        });
    }
    get title() {
        return _t("Content Catalog");
    }
    loadSubjects(filters) {
        return this.orm.call(
            "marketing.center.catalog.subject",
            "catalog_search_subjects",
            [],
            {company_id: this.props.companyId, ...filters}
        );
    }
    loadItems(subjectId, filters) {
        return this.orm.call(
            "marketing.center.catalog.item",
            "catalog_search_items",
            [],
            {company_id: this.props.companyId, subject_id: subjectId, ...filters}
        );
    }
    searchProducts(query) {
        return this.orm.call(
            "marketing.center.catalog.item",
            "catalog_search_products",
            [],
            {company_id: this.props.companyId, query}
        );
    }
}
CatalogBrowseDialog.template = "marketing_center_catalog.BrowseDialog";
CatalogBrowseDialog.components = {Dialog, CatalogBrowser};
CatalogBrowseDialog.props = {close: Function, companyId: Number, productIds: Array};

registry.category("actions").add("marketing_center_catalog.browser", (env, action) => {
    env.services.dialog.add(CatalogBrowseDialog, {
        companyId: action.params.company_id,
        productIds: action.params.product_ids || [],
    });
});
