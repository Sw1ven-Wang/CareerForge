/** Pi SDK controller. All inference and workspace tools cross the Python gateway. */
import { createInterface } from "node:readline";
import { createAgentSession, createExtensionRuntime, ModelRuntime, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream, getSystemMessageText } from "@earendil-works/pi-ai";

// The SDK must never call a provider or discover remote catalogs itself.
globalThis.fetch = async () => { throw new Error("Use the CareerForge model gateway"); };
const write = (value) => process.stdout.write(JSON.stringify(value) + "\n");
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
const pending = new Map();
let initialResolve;
const initial = new Promise(resolve => { initialResolve = resolve; });
let sequence = 0;
let initialized = false;
let session;
lines.on("line", line => {
  try {
    if (Buffer.byteLength(line) > 1000000) throw new Error("Frame too large");
    const value = JSON.parse(line);
    if (!initialized) { initialized = true; initialResolve(value); return; }
    const waiter = pending.get(value.id);
    if (!waiter) return;
    pending.delete(value.id);
    if (value.error) waiter.reject(new Error(value.error));
    else waiter.resolve(value.result);
  } catch { process.exitCode = 1; lines.close(); }
});
lines.on("close", () => {
  for (const waiter of pending.values()) waiter.reject(new Error("Gateway disconnected"));
  pending.clear();
  session?.abort().catch(() => {});
});
function rpc(type, payload) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    write({ type, id, ...payload });
  });
}
function text(content) {
  if (typeof content === "string") return content;
  return (content ?? []).filter(item => item.type === "text").map(item => item.text).join("\n");
}
function messagesFor(context) {
  const messages = [];
  if (context.systemPrompt) messages.push({ role: "system", content: context.systemPrompt });
  for (const entry of context.messages) {
    if (entry.role === "system") messages.push({ role: "system", content: getSystemMessageText({ ...entry, sections: Object.fromEntries(Object.entries(entry.sections ?? {}).filter(([name]) => name !== "cwd")) }) });
    else if (entry.role === "user") messages.push({ role: "user", content: text(entry.content) });
    else if (entry.role === "assistant") {
      const calls = entry.content.filter(item => item.type === "toolCall").map(item => ({
        id: item.id, type: "function", function: { name: item.name, arguments: JSON.stringify(item.arguments) },
      }));
      messages.push({ role: "assistant", content: text(entry.content) || null, ...(calls.length ? { tool_calls: calls } : {}) });
    } else if (entry.role === "toolResult") {
      messages.push({ role: "tool", tool_call_id: entry.toolCallId, content: text(entry.content) });
    } else throw new Error("Unsupported transcript entry");
  }
  return messages;
}
async function main() {
  const init = await initial;
  let proposed = false;
  let modelCalls = 0;
  const runtime = await ModelRuntime.create({
    authPath: process.cwd() + "/auth.json", modelsPath: null,
    modelsStorePath: process.cwd() + "/models.json", allowModelNetwork: false, refreshOnCreate: false,
  });
  // An inert SDK auth marker; the real credential remains in Python.
  await runtime.setRuntimeApiKey("openai", "careerforge-gateway-only");
  const resourceLoader = {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => init.system_prompt,
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
    extendResources: () => {}, reload: async () => {},
  };
  const customTools = init.tools.map(tool => ({
    ...tool, label: tool.name,
    async execute(toolCallId, args) {
      if (proposed) throw new Error("Proposal already submitted; human review required");
      const result = await rpc("tool", { name: tool.name, arguments: args, call_id: toolCallId });
      if (tool.name === "submit_reply" && !result.is_error) proposed = true;
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result, isError: !!result.is_error };
    },
  }));
  const model = {
    id: init.model, name: init.model, api: "openai-completions", provider: "openai",
    baseUrl: "http://127.0.0.1:9/disabled", reasoning: true, input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 128000, maxTokens: init.max_output_tokens,
  };
  ({ session } = await createAgentSession({
    cwd: process.cwd(), agentDir: process.cwd(), modelRuntime: runtime, model, thinkingLevel: "low",
    tools: customTools.map(tool => tool.name), customTools, resourceLoader,
    sessionManager: SessionManager.inMemory(process.cwd()),
    settingsManager: SettingsManager.inMemory({
      compaction: { enabled: false }, retry: { enabled: false, maxRetries: 0 }, cacheWarming: "off",
    }),
  }));
  session.agent.toolExecution = "sequential";
  session.agent.finishTurn = () => proposed || modelCalls >= init.call_limit ? { action: "end" } : undefined;
  session.agent.streamFunction = (_model, context) => {
    const stream = createAssistantMessageEventStream();
    const message = {
      role: "assistant", api: model.api, provider: model.provider, model: model.id,
      timestamp: Date.now(), content: [], stopReason: "stop",
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    };
    queueMicrotask(async () => {
      try {
        if (modelCalls >= init.call_limit) throw new Error("Model call budget exhausted");
        modelCalls++;
        const result = await rpc("model", { messages: messagesFor(context) });
        if (result.text) message.content.push({ type: "text", text: result.text });
        for (const call of result.tool_calls) message.content.push({ type: "toolCall", id: call.id, name: call.name, arguments: call.arguments });
        message.stopReason = result.finish_reason === "tool_calls" ? "toolUse" : "stop";
        // SDK counters are informational. Only Python's ModelCallReceipts account usage.
        message.usage.input = result.usage.input_tokens ?? 0;
        message.usage.output = result.usage.output_tokens ?? 0;
        message.usage.totalTokens = message.usage.input + message.usage.output;
        stream.push({ type: "start", partial: message });
        stream.push({ type: "done", reason: message.stopReason, message });
        stream.end(message);
      } catch (error) {
        message.stopReason = "error";
        message.errorMessage = String(error.message);
        stream.push({ type: "error", reason: "error", error: message });
        stream.end(message);
      }
    });
    return stream;
  };
  write({ type: "ready", sdk: "pi-coding-agent-1.3.0", tools: session.agent.state.tools.map(tool => tool.name) });
  try {
    await session.prompt(init.prompt, { expandPromptTemplates: false });
    const last = session.agent.state.messages.filter(entry => entry.role === "assistant").at(-1);
    write({ type: "done", proposed, calls: modelCalls, answer: text(last?.content),
      failed: last?.stopReason === "error" || last?.stopReason === "aborted",
      messages: session.agent.state.messages });
  } finally { session.dispose(); lines.close(); }
}
main().catch(error => { console.error(error.stack); write({ type: "failed", code: "PI_RUNTIME_ERROR" }); lines.close(); process.exitCode = 1; });
