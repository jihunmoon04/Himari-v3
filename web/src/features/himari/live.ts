// What a streamed Run is writing (GET /api/conversations/{id}/live, V3-P73 §6.1), folded into one state.
// Frames: snapshot (the whole state), delta (to append), end (that Run is recorded), idle, done (the job ended).

export interface LiveState {
  runId: string | null;
  text: string;
  tools: string[];
  thinking: boolean;
  done: boolean;
}

export const EMPTY_LIVE: LiveState = { runId: null, text: "", tools: [], thinking: false, done: false };

type Frame = { event: string; data: Record<string, unknown> };

const str = (value: unknown) => (typeof value === "string" ? value : "");
const strs = (value: unknown) => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : []);

export function reduceLive(state: LiveState, { event, data }: Frame): LiveState {
  switch (event) {
    case "snapshot":
      return {
        runId: str(data.run_id) || null,
        text: data.ended ? "" : str(data.text),
        tools: data.ended ? [] : strs(data.tools),
        thinking: !data.ended && Boolean(data.thinking),
        done: false,
      };
    case "delta":
      if (str(data.run_id) !== state.runId) {
        return { runId: str(data.run_id) || null, text: str(data.text), tools: strs(data.tools), thinking: Boolean(data.thinking), done: false };
      }
      return {
        ...state,
        text: state.text + str(data.text),
        tools: [...state.tools, ...strs(data.tools)],
        thinking: data.thinking === undefined ? state.thinking && !str(data.text) : Boolean(data.thinking),
      };
    case "end":
      // the Run's outcome is recorded: the timeline shows it from the events now
      return str(data.run_id) === state.runId ? { ...EMPTY_LIVE } : state;
    case "idle":
      return { ...EMPTY_LIVE };
    case "done":
      return { ...EMPTY_LIVE, done: true };
    default:
      return state;
  }
}
