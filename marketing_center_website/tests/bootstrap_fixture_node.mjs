// Load the actual shared dependency into isolated VM browser fixtures.
import fs from "node:fs";
import vm from "node:vm";

const source = fs
  .readFileSync(
    new URL("../static/src/js/bootstrap_config.esm.js", import.meta.url),
    "utf8"
  )
  .replace(/^export /gm, "");

export function installBootstrap(context, section = null) {
  const listeners = [];
  const doc = context.document;
  const add = doc.addEventListener;
  doc.addEventListener = (name, fn) => listeners.push([name, fn]);
  const exports = vm.runInContext(
    `(() => {${source}\nreturn {
    bootstrapGeneration, invalidateBootstrap, loadBootstrap, captureModeHint
  };})()`,
    context
  );
  doc.addEventListener = add;
  const dispatch = doc.dispatchEvent;
  doc.dispatchEvent = (event) => {
    for (const [name, fn] of listeners) if (name === event.type) fn(event);
    return dispatch && dispatch.call(doc, event);
  };
  if (section) {
    const fetch = context.window.fetch;
    context.window.fetch = (path, options) => {
      const result = fetch(path, options);
      if (!path.endsWith("bootstrap-config")) return result;
      return Promise.resolve(result).then((response) => ({
        ...response,
        json: async () => ({schema_version: 1, [section]: await response.json()}),
      }));
    };
  }
  Object.assign(context, exports);
}
