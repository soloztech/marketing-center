/** @odoo-module **/

import {
    Component,
    onMounted,
    onPatched,
    onWillDestroy,
    useRef,
    useState,
} from "@odoo/owl";
import {loadJS} from "@web/core/assets";
import {registry} from "@web/core/registry";
import {standardFieldProps} from "@web/views/fields/standard_field_props";

let pdfLibrary;
async function getPDFLibrary() {
    if (!pdfLibrary) {
        pdfLibrary = loadJS("/web/static/lib/pdfjs/build/pdf.js")
            .then(() => {
                const lib = window.pdfjsLib;
                lib.GlobalWorkerOptions.workerSrc =
                    "/web/static/lib/pdfjs/build/pdf.worker.js";
                return lib;
            })
            .catch((error) => {
                pdfLibrary = null;
                throw error;
            });
    }
    return pdfLibrary;
}

export class CatalogMediaPreview extends Component {
    setup() {
        this.root = useRef("root");
        this.canvas = useRef("canvas");
        this.state = useState({failed: false, ready: false});
        this.generation = 0;
        this.currentKey = null;
        onMounted(() => this.refresh());
        onPatched(() => this.refresh());
        onWillDestroy(() => this.cleanup());
    }

    get pdfUrl() {
        return `/web/static/lib/pdfjs/web/viewer.html?file=${encodeURIComponent(
            this.props.url
        )}#page=1`;
    }

    get icon() {
        return (
            {
                image: "fa-file-image-o",
                pdf: "fa-file-pdf-o",
                video: "fa-file-video-o",
                link: "fa-external-link",
                faq: "fa-question-circle-o",
                text: "fa-file-text-o",
            }[this.props.type] || "fa-file-o"
        );
    }

    cleanup() {
        this.generation++;
        if (this.videoElement) {
            this.videoElement.pause();
            this.videoElement.removeAttribute("src");
            this.videoElement.load();
            this.videoElement = null;
        }
        if (this.observer) {
            this.observer.disconnect();
            this.observer = null;
        }
        if (this.renderTask) {
            this.renderTask.cancel();
            this.renderTask = null;
        }
        if (this.loadingTask) {
            this.loadingTask.destroy().catch(() => {
                // Cancellation may race a failed document fetch.
            });
            this.loadingTask = null;
        }
    }

    refresh() {
        const key = `${this.props.type}:${this.props.url}:${Boolean(this.props.large)}`;
        if (key === this.currentKey) {
            return;
        }
        this.cleanup();
        this.currentKey = key;
        this.state.failed = false;
        this.state.ready = false;
        this.videoElement = this.root.el.querySelector("video");
        if (this.videoElement) this.videoElement.src = this.props.url;
        if (this.props.type === "pdf" && this.props.url && !this.props.large) {
            const generation = this.generation;
            const url = this.props.url;
            const observer = new IntersectionObserver(
                (entries) => {
                    if (generation !== this.generation) return;
                    if (entries.some((entry) => entry.isIntersecting)) {
                        observer.disconnect();
                        this.renderPDF(generation, url);
                    }
                },
                {rootMargin: "100px"}
            );
            this.observer = observer;
            observer.observe(this.root.el);
        }
    }

    async renderPDF(generation, url) {
        try {
            const lib = await getPDFLibrary();
            if (generation !== this.generation) return;
            this.loadingTask = lib.getDocument({url, isEvalSupported: false});
            const document = await this.loadingTask.promise;
            const page = await document.getPage(1);
            if (generation !== this.generation || !this.canvas.el) return;
            const viewport = page.getViewport({scale: 1});
            const scaled = page.getViewport({
                scale: Math.min(360 / viewport.width, 1.5),
            });
            const canvas = this.canvas.el;
            canvas.width = scaled.width;
            canvas.height = scaled.height;
            this.renderTask = page.render({
                canvasContext: canvas.getContext("2d"),
                viewport: scaled,
            });
            await this.renderTask.promise;
            if (generation === this.generation) this.state.ready = true;
        } catch (_error) {
            if (generation === this.generation) this.state.failed = true;
        }
    }
}

CatalogMediaPreview.template = "marketing_center_catalog.MediaPreview";
CatalogMediaPreview.props = {
    type: {type: String, optional: true},
    url: {type: [String, Boolean], optional: true},
    name: {type: String, optional: true},
    large: {type: Boolean, optional: true},
};

export class CatalogMediaPreviewField extends Component {
    get previewUrl() {
        const record = this.props.record;
        if (!record.resId || record.isDirty) return false;
        const stamp = encodeURIComponent(String(record.data.write_date || ""));
        return `/marketing_center/catalog/item/${record.resId}/preview?unique=${stamp}`;
    }
}
CatalogMediaPreviewField.template = "marketing_center_catalog.MediaPreviewField";
CatalogMediaPreviewField.components = {CatalogMediaPreview};
CatalogMediaPreviewField.props = {
    ...standardFieldProps,
    large: {type: Boolean, optional: true},
};
CatalogMediaPreviewField.fieldDependencies = {write_date: {type: "datetime"}};
CatalogMediaPreviewField.supportedTypes = ["selection", "char"];
CatalogMediaPreviewField.extractProps = ({attrs}) => ({
    large: Boolean(attrs.options.large),
});
registry.category("fields").add("catalog_media_preview", CatalogMediaPreviewField);
