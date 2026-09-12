/** @odoo-module **/

import {ListController} from "@web/views/list/list_controller";
import {listView} from "@web/views/list/list_view";
import {registry} from "@web/core/registry";

export class MetaLeadRouteListController extends ListController {
    async onDiscoverForms() {
        return this.actionService.doAction(
            "marketing_center_meta.action_meta_lead_discovery",
            {onClose: () => this.model.load()}
        );
    }
}

registry.category("views").add("marketing_meta_lead_routes", {
    ...listView,
    Controller: MetaLeadRouteListController,
    buttonTemplate: "marketing_center_meta.LeadRouteList.Buttons",
});
