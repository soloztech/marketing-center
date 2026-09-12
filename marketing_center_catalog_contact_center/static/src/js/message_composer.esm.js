/** @odoo-module **/

import {onWillDestroy, useChildSubEnv} from "@odoo/owl";
import {CatalogPanel} from "./catalog_dialog.esm";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
import {MessageComposer} from "@contact_center_ui/js/message_composer.esm";
import {_t} from "@web/core/l10n/translation";
import {patch} from "@web/core/utils/patch";

export function appendCatalogText(body, texts, maxLength) {
    const addition = texts
        .filter((text) => typeof text === "string" && text)
        .join("\n\n");
    const next = body + (body && addition ? "\n\n" : "") + addition;
    if (next.length > maxLength) {
        throw new Error(_t("The selected text exceeds the draft length limit."));
    }
    return next;
}

export function catalogContextCurrent(composer, context) {
    return Boolean(
        composer.draftSourceCurrent(context.source, context.generation) &&
            !composer.noteMode &&
            !composer.scheduleMode &&
            !composer.local.structuredDraft &&
            !composer.local.sendPlan &&
            !composer.local.actionBusy &&
            !composer.voiceCaptureActive &&
            !composer.sending
    );
}

// Returns IDs already inserted so that retrying a failed file cannot repeat text
// or files successfully prepared before it. Native uploads retain their own errors.
export async function addCatalogItems(
    composer,
    context,
    payload,
    isAlive = () => true
) {
    const inserted = [];
    if (
        !isAlive() ||
        payload.channel_id !== context.source.channelId ||
        !catalogContextCurrent(composer, context)
    ) {
        return inserted;
    }
    const textItems = payload.items.filter((item) => !item.file);
    composer.local.body = appendCatalogText(
        composer.local.body,
        textItems.map((item) => item.text),
        composer.bodyMaxLength
    );
    composer.persistDraft(composer.activeChannelId);
    composer.resize();
    inserted.push(...textItems.map((item) => item.id));
    for (const item of payload.items.filter((entry) => entry.file)) {
        if (!isAlive() || !catalogContextCurrent(composer, context)) {
            break;
        }
        const before = new Set(
            (composer.attachments || []).map((attachment) => attachment.id)
        );
        let success = false;
        try {
            success = await composer.store.addDraftAttachment({
                ...item.file,
                channelId: context.source.channelId,
                customerKey: context.source.customerKey,
            });
        } catch (_error) {
            // Preserve the successful part and let the normal composer show errors.
        }
        const sameDraft = composer.draftSourceCurrent(
            context.source,
            context.generation
        );
        const prepared =
            sameDraft &&
            (composer.attachments || []).some(
                (attachment) => !before.has(attachment.id)
            );
        if (success || prepared) {
            // A failed native upload may already have an error card with Retry.
            // Do not offer that same file for a second, duplicate catalog upload.
            inserted.push(item.id);
        }
        if (!success) {
            break;
        }
    }
    return inserted;
}

patch(MessageComposer.prototype, "marketing_center_catalog_contact_center.composer", {
    setup() {
        this._super(...arguments);
        if (this.env.contentCatalog) {
            const unregister = this.env.contentCatalog.registerComposer(this);
            onWillDestroy(unregister);
        }
    },
});

patch(ContactCenterApp, "marketing_center_catalog_contact_center.components", {
    components: {...ContactCenterApp.components, CatalogPanel},
});

patch(ContactCenterApp.prototype, "marketing_center_catalog_contact_center.panel", {
    setup() {
        this._super(...arguments);
        this.catalogComposer = null;
        useChildSubEnv({
            contentCatalog: {
                registerComposer: (composer) => {
                    this.catalogComposer = composer;
                    return () => {
                        if (this.catalogComposer === composer) {
                            this.catalogComposer = null;
                        }
                    };
                },
            },
        });
    },

    get canViewCatalog() {
        return Boolean(
            this.selectedConversation && this.store.capabilities.content_catalog
        );
    },

    get catalogPanelSelected() {
        return this.canViewCatalog && this.ui.sidePanel === "catalog";
    },

    toggleCatalogPanel() {
        if (this.canViewCatalog) {
            this.toggleSidePanel("catalog");
        }
    },

    closeCatalogPanel() {
        this.store.state.detailsOpen = false;
        const trigger = document.querySelector(
            ".o_contact_center_ui .cc-catalog-toggle"
        );
        if (trigger) {
            trigger.focus();
        }
    },

    getCatalogDraft() {
        const composer = this.catalogComposer;
        if (!composer || !this.canViewCatalog) {
            return null;
        }
        const context = {
            source: composer.draftSourceContext(),
            generation: composer.uploadGeneration,
        };
        const current = () =>
            this.catalogComposer === composer &&
            this.catalogPanelSelected &&
            this.store.state.detailsOpen &&
            catalogContextCurrent(composer, context);
        return {
            canAdd: current,
            onAdd: (payload, isAlive) =>
                addCatalogItems(
                    composer,
                    context,
                    payload,
                    () => isAlive() && current()
                ),
        };
    },
});
