/** @odoo-module **/

import {onWillDestroy, useState} from "@odoo/owl";
import {FormArchParser} from "@web/views/form/form_arch_parser";
import {X2ManyField} from "@web/views/fields/x2many/x2many_field";
import {formView} from "@web/views/form/form_view";
import {registry} from "@web/core/registry";

/**
 * Keep both inline renderers available without reloading the relation. Loading
 * their combined fields up front also preserves virtual records and pending
 * parent-form changes when the user changes the presentation.
 */
export class CatalogSubjectFormArchParser extends FormArchParser {
    parse(...args) {
        const archInfo = super.parse(...args);
        for (const fieldInfo of Object.values(archInfo.fieldNodes)) {
            if (fieldInfo.widget !== "catalog_x2many") continue;
            const {views} = fieldInfo;
            if (!views.list || !views.kanban) {
                throw new Error("catalog_x2many requires inline tree and kanban views");
            }
            const defaultView =
                fieldInfo.options.default_view === "kanban" ? "kanban" : "list";
            const otherView = defaultView === "list" ? "kanban" : "list";
            const activeFields = {
                ...views[otherView].activeFields,
                ...views[defaultView].activeFields,
            };
            for (const mode of ["list", "kanban"]) {
                views[mode] = {...views[mode], activeFields};
            }
            // Explicit mode prevents Odoo's responsive subview loader from
            // replacing the requested default with a viewport-based choice.
            fieldInfo.viewMode = defaultView;
        }
        return archInfo;
    }
}

export class CatalogX2ManyField extends X2ManyField {
    setup() {
        super.setup();
        this.switchState = useState({busy: false});
        this.destroyed = false;
        onWillDestroy(() => {
            this.destroyed = true;
        });
    }

    async switchView(mode) {
        if (
            this.switchState.busy ||
            mode === this.viewMode ||
            !["list", "kanban"].includes(mode)
        ) {
            return;
        }
        this.switchState.busy = true;
        try {
            const editedRecord = this.list.editedRecord;
            if (editedRecord) {
                // Html/text editors may still hold input locally. Flush them
                // before unmounting the list, without saving the parent form.
                await editedRecord.askChanges();
                if (this.destroyed || !(await editedRecord.checkValidity())) return;
                const switched = await editedRecord.switchMode("readonly", {
                    checkValidity: true,
                });
                if (this.destroyed || switched === false) return;
            }
            if (!this.destroyed) this.viewMode = mode;
        } finally {
            if (!this.destroyed) this.switchState.busy = false;
        }
    }
}

CatalogX2ManyField.template = "marketing_center_catalog.X2ManyField";
registry.category("fields").add("catalog_x2many", CatalogX2ManyField);
registry.category("views").add("catalog_subject_form", {
    ...formView,
    ArchParser: CatalogSubjectFormArchParser,
});
