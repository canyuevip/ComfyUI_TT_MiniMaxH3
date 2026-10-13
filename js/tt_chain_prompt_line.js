import { app } from "../../scripts/app.js";

// H3 Chain Prompt Line
// Before every prompt is queued, read the H3 Motion Context Save Latent
// `clip_index` and push it (minus one) into every TTChainPromptLine node's
// `start_index`. Run/Re-roll does not change clip_index, so it re-runs the
// same row; Approve/Chain advance clip_index, so the next row is used.

const LOAD_CLASS = "TTH3MotionContextLoadLatent";
const SAVE_CLASS = "TTH3MotionContextSaveLatent";
const LINE_CLASS = "TTChainPromptLine";

function currentRow() {
  // Prefer the Load node: it tells us which segment is *actually* being run now,
  // so Run/Re-roll / ComfyUI Run re-sync the prompt row with the Loaded clip.
  // Fall back to Save only when there is no Load node on the graph.
  let loadIndex = -1;
  let saveIndex = 0;
  let foundLoad = false;
  let foundSave = false;
  const nodes = app.graph?._nodes || [];
  for (const n of nodes) {
    if (n?.comfyClass === LOAD_CLASS) {
      const w = n.widgets?.find((x) => x.name === "clip_index");
      if (w) {
        foundLoad = true;
        loadIndex = Math.max(loadIndex, w.value | 0);
      }
    }
    if (n?.comfyClass === SAVE_CLASS) {
      const w = n.widgets?.find((x) => x.name === "clip_index");
      if (w) {
        foundSave = true;
        saveIndex = Math.max(saveIndex, w.value | 0);
      }
    }
  }
  if (foundLoad) return Math.max(0, loadIndex);
  if (foundSave) return Math.max(0, saveIndex - 1);
  return null;
}

function syncStartIndex() {
  let row;
  try {
    row = currentRow();
  } catch (e) {
    return;
  }
  if (row === null) return; // standalone use: respect manual start_index
  for (const n of app.graph?._nodes || []) {
    if (n?.comfyClass !== LINE_CLASS) continue;
    const w = n.widgets?.find((x) => x.name === "start_index");
    if (w) w.value = row;
  }
}

app.registerExtension({
  name: "tt.chain_prompt_line",

  setup() {
    const orig = app.queuePrompt?.bind(app);
    if (typeof orig !== "function") return;
    app.queuePrompt = async function () {
      try { syncStartIndex(); } catch (e) {
        console.warn("[H3 Chain Prompt Line] sync failed", e);
      }
      return orig(...arguments);
    };
  },

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== LINE_CLASS) return;
    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onCreated?.apply(this, arguments);
      syncStartIndex();
      return result;
    };
  },
});
