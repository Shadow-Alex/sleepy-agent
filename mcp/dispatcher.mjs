import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { NativePipeClient } from "./native_pipe_client.mjs";

const BACKEND = "codex_desktop_native_pipe";
const SERVER_VERSION = "0.4.0";
const MAX_STDIO_MESSAGE_BYTES = 8 * 1024 * 1024;
const DISPATCH_INTERVAL_MS = 2_000;
const HEARTBEAT_INTERVAL_MS = 5_000;
const stateRoot =
  process.env.DURABLE_CONTINUE_HOME ||
  path.join(process.env.HOME || "", ".codex", "durable-continue");
const python = path.join(stateRoot, "venv", "bin", "python");
const pipePath = process.env.CODEX_APP_TOOLS_PIPE_PATH?.trim() || "";
const nativeClient = pipePath ? new NativePipeClient(pipePath) : null;
const dispatchDisabled = process.env.DURABLE_CONTINUE_DISABLE_DISPATCH === "1";
const dispatcherStatusDirectory = path.join(stateRoot, "dispatchers");
const dispatcherStatusPath = path.join(
  dispatcherStatusDirectory,
  `${process.pid}.json`,
);
const dispatcherStartedAt = new Date().toISOString();

let initialized = false;
let dispatching = false;
let inputBuffer = Buffer.alloc(0);

process.stdin.on("data", (chunk) => {
  inputBuffer = Buffer.concat([inputBuffer, chunk]);
  if (inputBuffer.length > MAX_STDIO_MESSAGE_BYTES) {
    log("MCP input exceeded safety bound");
    process.exit(2);
  }
  while (true) {
    const newline = inputBuffer.indexOf("\n");
    if (newline < 0) break;
    const line = inputBuffer.subarray(0, newline).toString("utf8").replace(/\r$/, "");
    inputBuffer = inputBuffer.subarray(newline + 1);
    if (!line.trim()) continue;
    try {
      handleMcpMessage(JSON.parse(line));
    } catch (error) {
      log(`invalid MCP message: ${safeError(error)}`);
    }
  }
});

process.stdin.on("end", shutdown);
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

const dispatchTimer = setInterval(() => {
  void dispatchOnce();
}, DISPATCH_INTERVAL_MS);
const heartbeatTimer = setInterval(writeHeartbeat, HEARTBEAT_INTERVAL_MS);

function writeHeartbeat() {
  if (!initialized) return;
  try {
    fs.mkdirSync(dispatcherStatusDirectory, { recursive: true, mode: 0o700 });
    const temporary = `${dispatcherStatusPath}.${process.pid}.tmp`;
    fs.writeFileSync(
      temporary,
      JSON.stringify({
        backend: BACKEND,
        pid: process.pid,
        parentPid: process.ppid,
        version: SERVER_VERSION,
        startedAt: dispatcherStartedAt,
        updatedAt: new Date().toISOString(),
        nativePipeAvailable: nativeClient != null,
        stateRuntimeAvailable: fs.existsSync(python),
      }) + "\n",
      { encoding: "utf8", mode: 0o600 },
    );
    fs.renameSync(temporary, dispatcherStatusPath);
  } catch (error) {
    log(`could not write dispatcher heartbeat: ${safeError(error)}`);
  }
}

function handleMcpMessage(message) {
  const method = message?.method;
  if (method === "initialize") {
    initialized = true;
    writeHeartbeat();
    respond(message.id, {
      protocolVersion: message.params?.protocolVersion || "2025-11-25",
      capabilities: { tools: {} },
      serverInfo: {
        name: "durable-continue-dispatcher",
        title: "Sleepy Agent Dispatcher",
        version: SERVER_VERSION,
      },
      instructions:
        "Sleepy Agent's Desktop-owned dispatcher for exact same-task continue delivery.",
    });
    return;
  }
  if (method === "notifications/initialized" || method === "notifications/cancelled") {
    return;
  }
  if (method === "ping") {
    respond(message.id, {});
    return;
  }
  if (method === "tools/list") {
    respond(message.id, {
      tools: [
        {
          name: "durable_continue_dispatcher_status",
          description:
            "Report whether the Desktop-owned durable continue dispatcher is available.",
          inputSchema: {
            type: "object",
            additionalProperties: false,
            properties: {},
          },
        },
      ],
    });
    return;
  }
  if (method === "tools/call") {
    if (message.params?.name !== "durable_continue_dispatcher_status") {
      fail(message.id, -32602, "Unknown durable-continue tool");
      return;
    }
    respond(message.id, {
      content: [
        {
          type: "text",
          text: JSON.stringify({
            backend: BACKEND,
            nativePipeAvailable: nativeClient != null,
            stateRuntimeAvailable: fs.existsSync(python),
            dispatchDisabled,
          }),
        },
      ],
      isError: false,
    });
    return;
  }
  if (message.id != null) fail(message.id, -32601, "Method not found");
}

async function dispatchOnce() {
  if (
    dispatchDisabled ||
    !initialized ||
    dispatching ||
    nativeClient == null ||
    !fs.existsSync(python)
  ) {
    return;
  }
  dispatching = true;
  let claim = null;
  try {
    const claimed = await runStateCommand(["_dispatcher-claim"]);
    claim = claimed?.claim ?? null;
    if (claim == null) return;

    const before = await observe(claim, 0);
    if (before?.observed || before?.stale) return;

    let nativeError = null;
    try {
      await nativeClient.sendContinue(claim);
    } catch (error) {
      nativeError = error;
    }

    const after = await observe(claim, nativeError == null ? 30 : 2);
    if (after?.observed || after?.stale) return;

    const reason = nativeError == null ? "send_unconfirmed" : "native_pipe_error";
    const detail = nativeError == null
      ? "Desktop native send returned but exact continue was not observed in rollout"
      : safeError(nativeError);
    await failClaim(claim, detail, reason);
  } catch (error) {
    log(`dispatch failed: ${safeError(error)}`);
    if (claim != null) {
      try {
        await failClaim(claim, safeError(error), "dispatcher_error");
      } catch (recordError) {
        log(`could not record dispatch failure: ${safeError(recordError)}`);
      }
    }
  } finally {
    dispatching = false;
  }
}

function observe(claim, waitSeconds) {
  return runStateCommand([
    "_dispatcher-observe",
    claim.monitor_id,
    "--claim-token",
    claim.claim_token,
    "--wait-seconds",
    String(waitSeconds),
  ], (waitSeconds + 10) * 1_000);
}

function failClaim(claim, error, reason) {
  return runStateCommand([
    "_dispatcher-fail",
    claim.monitor_id,
    "--claim-token",
    claim.claim_token,
    "--error",
    String(error).slice(0, 2_000),
    "--reason",
    reason,
  ]);
}

function runStateCommand(argumentsValue, timeoutMs = 20_000) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      python,
      ["-m", "durable_continue.cli", ...argumentsValue],
      {
        env: {
          ...process.env,
          DURABLE_CONTINUE_HOME: stateRoot,
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`state command timed out: ${argumentsValue[0]}`));
    }, Math.max(1_000, timeoutMs));
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout = boundedAppend(stdout, chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderr = boundedAppend(stderr, chunk);
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(
          new Error(
            `state command ${argumentsValue[0]} exited ${code}: ${stderr.trim()}`,
          ),
        );
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch {
        reject(new Error(`state command ${argumentsValue[0]} returned invalid JSON`));
      }
    });
  });
}

function boundedAppend(existing, chunk) {
  const combined = existing + chunk;
  return combined.length <= 64_000 ? combined : combined.slice(-64_000);
}

function respond(id, result) {
  writeMessage({ jsonrpc: "2.0", id, result });
}

function fail(id, code, message) {
  writeMessage({ jsonrpc: "2.0", id, error: { code, message } });
}

function writeMessage(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function safeError(error) {
  const text = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  const redacted = pipePath
    ? text.replaceAll(pipePath, "<desktop-native-pipe>")
    : text;
  return redacted.slice(0, 2_000);
}

function log(message) {
  process.stderr.write(`[durable-continue] ${message}\n`);
}

function shutdown() {
  clearInterval(dispatchTimer);
  clearInterval(heartbeatTimer);
  try {
    fs.unlinkSync(dispatcherStatusPath);
  } catch (error) {
    if (error?.code !== "ENOENT") {
      log(`could not remove dispatcher heartbeat: ${safeError(error)}`);
    }
  }
  nativeClient?.close();
  process.exit(0);
}
