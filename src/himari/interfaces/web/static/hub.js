/* The hub's waiting requests (V3-P95): what the coding agents HIMARI handed work to wait on the user for.
 *
 * A tray at the page's corner, shown only while something waits: a permission (the agent wants to write a
 * file or run a command) with the agent's own choices, or a question with its fields. The answer goes to
 * HIMARI's server, which passes it to the hub with the user token; the model takes no part in it.
 *
 * Hostile content: every text here was written by an agent. It is set with textContent on elements made by
 * createElement; nothing is parsed as HTML.
 *
 * The list is read every few seconds while the page shows. A server without a hub says so once and is not
 * asked again until the next sign-in.
 */

const QUIET_MS = 4000; // between reads while nothing waits
const BUSY_MS = 2000; // … while something does (an answered request leaves, the next one arrives)

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export class HubTray {
  /* ``api(method, path, body)``: the page's request function (it shows the sign-in on a 401). */
  constructor(api) {
    this.api = api;
    this.timer = null;
    this.ticket = 0; // a read answered after a stop, or after a newer read, is dropped
    this.running = false;
    this.sending = new Set(); // request ids whose answer is on its way: their buttons are off
    this.drafts = new Map(); // request id -> {field name -> value}: a question's answers as typed so far
    this.problem = new Map(); // request id -> why the last answer was not taken
    this.shown = ""; // what the tray draws now, to leave it alone when nothing changed (typing, focus)
    this.root = el("aside", "hub-tray");
    this.root.hidden = true;
    this.root.setAttribute("aria-label", "Agent requests");
    this.root.setAttribute("aria-live", "polite");
    document.body.append(this.root);
    document.addEventListener("visibilitychange", () => {
      if (this.running && !document.hidden) this.schedule(0);
    });
  }

  start() {
    this.running = true;
    this.schedule(0);
  }

  stop() {
    this.running = false;
    this.ticket += 1;
    clearTimeout(this.timer);
    this.sending.clear();
    this.drafts.clear();
    this.problem.clear();
    this.draw([], true);
  }

  schedule(delay) {
    clearTimeout(this.timer);
    if (this.running) this.timer = setTimeout(() => this.read(), delay);
  }

  async read() {
    if (document.hidden) return; // read again when the page shows
    const ticket = (this.ticket += 1);
    let answer;
    try {
      answer = await this.api("GET", "/api/hub/pending");
    } catch (error) {
      if (ticket === this.ticket && this.running) this.schedule(QUIET_MS * 2);
      return;
    }
    if (ticket !== this.ticket || !this.running) return;
    if (!answer.enabled) {
      this.running = false; // no hub on this server
      this.draw([], true);
      return;
    }
    const pending = Array.isArray(answer.pending) ? answer.pending : [];
    const waiting = new Set(pending.map((item) => item.id));
    for (const id of [...this.drafts.keys()]) if (!waiting.has(id)) this.drafts.delete(id);
    for (const id of [...this.problem.keys()]) if (!waiting.has(id)) this.problem.delete(id);
    this.draw(pending, answer.reachable);
    this.schedule(pending.length ? BUSY_MS : QUIET_MS);
  }

  draw(pending, reachable) {
    const shown = JSON.stringify([pending, reachable, [...this.sending], [...this.problem]]);
    if (shown === this.shown) return;
    this.shown = shown;
    this.root.replaceChildren();
    this.root.hidden = pending.length === 0;
    if (!pending.length) return;
    const head = el("div", "hub-tray-head");
    head.append(el("span", "hub-tray-dot"), el("span", "hub-tray-title",
      pending.length === 1 ? "An agent is waiting for you" : `${pending.length} agent requests are waiting`));
    this.root.append(head);
    for (const item of pending) this.root.append(this.card(item));
  }

  card(item) {
    const card = el("section", "hub-card");
    const task = item.task || {};
    const where = [task.project, task.agent, task.runner && task.runner !== "local" ? `on ${task.runner}` : null]
      .filter(Boolean).join(" · ");
    card.append(el("div", "hub-card-where", where || `task ${item.task_id}`));
    if (task.prompt) card.append(el("div", "hub-card-prompt", task.prompt));
    card.append(el("div", "hub-card-asks", item.asks || (item.kind === "permission" ? "Permission" : "Question")));
    const busy = this.sending.has(item.id);
    const actions = el("div", "hub-card-actions");
    if (item.kind === "permission") {
      for (const option of item.options || []) {
        const allow = String(option.kind || "").startsWith("allow");
        const button = el("button", allow ? "primary-button hub-button" : "hub-button", option.name || option.option_id);
        button.type = "button";
        button.disabled = busy;
        button.addEventListener("click", () => this.answer(item.id, { option_id: option.option_id }));
        actions.append(button);
      }
    } else {
      card.append(this.form(item));
      const send = el("button", "primary-button hub-button", "Answer");
      send.type = "button";
      send.disabled = busy;
      send.addEventListener("click", () => this.answer(item.id, { content: this.content(item) }));
      actions.append(send);
    }
    // The agent's own "reject" choice is the refusal when it gave one: a second button would say the same.
    const rejects = (item.options || []).some((option) => String(option.kind || "").startsWith("reject"));
    const refuse = el("button", "hub-button hub-button-quiet", item.kind === "permission" ? "Refuse" : "Decline");
    refuse.type = "button";
    refuse.disabled = busy;
    refuse.hidden = rejects;
    refuse.addEventListener("click", () => this.answer(item.id, { refuse: true }));
    const stop = el("button", "hub-button hub-button-quiet", "Stop task");
    stop.type = "button";
    stop.disabled = busy;
    stop.addEventListener("click", () => this.cancel(item));
    actions.append(refuse, stop);
    card.append(actions);
    const problem = this.problem.get(item.id);
    if (problem) card.append(el("div", "hub-card-problem", problem));
    return card;
  }

  /* A question's fields: a choice list where the agent gave choices, a line of text where it gave none. */
  form(item) {
    const form = el("div", "hub-form");
    const draft = this.drafts.get(item.id) || {};
    this.drafts.set(item.id, draft);
    for (const field of item.fields || []) {
      const row = el("label", "hub-field");
      row.append(el("span", "hub-field-title", field.title || field.name));
      if (field.description) row.append(el("span", "hub-field-note", field.description));
      if ((field.choices || []).length) {
        const select = el("select", "hub-input");
        select.multiple = field.multiple;
        if (!field.multiple) select.append(new Option("Choose…", ""));
        const chosen = draft[field.name];
        for (const choice of field.choices) {
          const option = new Option(choice.title || String(choice.value), String(choice.value));
          option.title = choice.description || "";
          option.selected = field.multiple ? Array.isArray(chosen) && chosen.includes(String(choice.value))
            : chosen === String(choice.value);
          select.append(option);
        }
        select.addEventListener("change", () => {
          draft[field.name] = field.multiple ? [...select.selectedOptions].map((o) => o.value) : select.value;
        });
        row.append(select);
      } else {
        const input = el("input", "hub-input");
        input.type = "text";
        input.value = typeof draft[field.name] === "string" ? draft[field.name] : "";
        input.addEventListener("input", () => { draft[field.name] = input.value; });
        row.append(input);
      }
      form.append(row);
    }
    return form;
  }

  /* The answer as the agent's form asks for it: only the fields that were filled, choices as their own
   * values (a number stays a number). */
  content(item) {
    const draft = this.drafts.get(item.id) || {};
    const content = {};
    for (const field of item.fields || []) {
      const value = draft[field.name];
      const real = (text) => {
        const choice = (field.choices || []).find((c) => String(c.value) === text);
        return choice ? choice.value : text;
      };
      if (Array.isArray(value)) {
        if (value.length) content[field.name] = value.map(real);
      } else if (typeof value === "string" && value !== "") {
        content[field.name] = real(value);
      }
    }
    return content;
  }

  async answer(id, body) {
    await this.send(id, () => this.api("POST", `/api/hub/pending/${id}/answer`, body));
  }

  async cancel(item) {
    if (!window.confirm("Stop this agent's task? What it has done so far stays.")) return;
    await this.send(item.id, () => this.api("POST", `/api/hub/tasks/${item.task_id}/cancel`));
  }

  /* One answer at a time for a request; it is never sent again by itself. What the server says of a
   * failure is shown on the card, and the list is read again either way. */
  async send(id, call) {
    if (this.sending.has(id)) return;
    this.sending.add(id);
    this.problem.delete(id);
    this.shown = ""; // draw the buttons off on the next read
    try {
      await call();
      this.drafts.delete(id);
    } catch (error) {
      this.problem.set(id, error && error.message ? error.message : "The answer could not be sent.");
    } finally {
      this.sending.delete(id);
      this.shown = "";
      if (this.running) this.schedule(0);
    }
  }
}
