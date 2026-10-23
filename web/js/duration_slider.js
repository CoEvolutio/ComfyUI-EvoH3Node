// `duration` means two different things, so the widget follows long_mode.
//
//   long_mode = off          duration is SECONDS, capped at 10 (the int8
//                            divergence cliff for a single pass)
//   long_mode = autocontext  duration is a SEGMENT COUNT; total length is
//                            segments * chunk_seconds, capped by the plugin's
//                            total_frames grid (16376 / 24fps = 682 s)
//
// Counting segments is the whole point: 75 x 8s = 10 min is easier to reason
// about than 600 s, and one multiply cannot silently produce a wrong length.
// The range, the label and the clamping all move together.
import { app } from "../../scripts/app.js";

const NODE_TYPES = new Set([
  "EvoH3FirstLastFrameNode",
  "EvoH3ReferenceImagesNode",
  "EvoH3ReferenceVoiceNode",
]);

const SHORT_MAX = 10;
const TOTAL_SECONDS_CEILING = 682; // 16376 frames / 24 fps
const LABEL_SHORT = "duration（秒）";
const LABEL_LONG = "duration（段数）";

function isLongMode(value) {
  return String(value ?? "").toLowerCase().startsWith("autocontext");
}

function setWidgetMax(widget, max) {
  widget.options = widget.options || {};
  widget.options.max = max;
  if (typeof widget.value === "number" && widget.value > max) {
    widget.value = max;
    widget.callback?.(max);
  }
  // New (DOM) frontend keeps its own attributes on the range/number element.
  const holders = [widget.element, widget.inputEl, widget.domElement].filter(Boolean);
  for (const el of holders) {
    if (el.tagName === "INPUT" && el.max !== undefined) el.max = String(max);
    const inner = el.querySelector?.("input[type=range], input[type=number]");
    if (inner) {
      inner.max = String(max);
      if (Number(inner.value) > max) inner.value = String(max);
    }
  }
}

function wire(node) {
  const longW = node.widgets?.find((w) => w.name === "long_mode");
  const durW = node.widgets?.find((w) => w.name === "duration");
  if (!longW || !durW) return;
  const chunkW = node.widgets?.find((w) => w.name === "chunk_seconds");

  const sync = () => {
    const long = isLongMode(longW.value);
    const chunk = Math.max(1, Number(chunkW?.value) || 8);
    const max = long ? Math.max(1, Math.floor(TOTAL_SECONDS_CEILING / chunk)) : SHORT_MAX;
    setWidgetMax(durW, max);
    const label = long ? LABEL_LONG : LABEL_SHORT;
    if (durW.label !== label) durW.label = label;
    node.setDirtyCanvas?.(true, true);
    app.graph?.setDirtyCanvas?.(true, true);
  };

  for (const w of [longW, chunkW]) {
    if (!w || w.__evoh3Wrapped) continue;
    const original = w.callback;
    w.callback = function (...args) {
      const out = original?.apply(this, args);
      sync();
      return out;
    };
    w.__evoh3Wrapped = true;
  }
  sync();
}

app.registerExtension({
  name: "EvoH3H3.durationSlider",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!NODE_TYPES.has(nodeData?.name)) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const out = onNodeCreated?.apply(this, arguments);
      wire(this);
      return out;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const out = onConfigure?.apply(this, arguments);
      wire(this);
      return out;
    };
  },
});
