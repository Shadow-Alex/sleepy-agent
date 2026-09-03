import net from "node:net";
import path from "node:path";

const MAX_FRAME_BYTES = 8 * 1024 * 1024;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export class NativePipeClient {
  constructor(pipePath) {
    if (
      typeof pipePath !== "string" ||
      !path.isAbsolute(pipePath) ||
      !(
        pipePath.startsWith("/tmp/codex-browser-use/") ||
        pipePath.startsWith("/private/tmp/codex-browser-use/")
      ) ||
      !pipePath.endsWith(".sock")
    ) {
      throw new Error("Codex Desktop did not provide a valid native pipe path");
    }
    this.pipePath = pipePath;
    this.socket = null;
    this.connecting = null;
    this.pendingData = Buffer.alloc(0);
    this.pending = new Map();
    this.nextId = 1;
    this.tools = null;
  }

  async sendContinue(claim) {
    validateClaim(claim);
    const tools = await this.listTools();
    const tool = tools.find(
      (candidate) =>
        candidate?.name === "send_message_to_thread" &&
        candidate?.namespace === "codex_app",
    );
    if (!tool) {
      throw new Error("Codex Desktop does not expose send_message_to_thread");
    }
    const result = await this.request(
      "tools/call",
      {
        arguments: {
          threadId: claim.thread_id,
          prompt: "continue",
        },
        callId: claim.call_id,
        namespace: "codex_app",
        threadId: claim.thread_id,
        tool: "send_message_to_thread",
        turnId: claim.context_turn_id,
      },
      30_000,
    );
    if (result?.success !== true) {
      throw new Error("Codex Desktop rejected the durable continue message");
    }
    return result;
  }

  async listTools() {
    if (this.tools != null) return this.tools;
    const result = await this.request(
      "tools/list",
      { threadStartKind: "all" },
      10_000,
    );
    if (!Array.isArray(result?.tools)) {
      throw new Error("Codex Desktop returned an invalid tool catalog");
    }
    this.tools = result.tools;
    return this.tools;
  }

  async request(method, params, timeoutMs) {
    await this.connect();
    const socket = this.socket;
    if (socket == null || socket.destroyed) {
      throw new Error("Codex Desktop native pipe is closed");
    }
    const id = this.nextId++;
    const payload = Buffer.from(
      JSON.stringify({ jsonrpc: "2.0", id, method, params }),
      "utf8",
    );
    if (payload.length > MAX_FRAME_BYTES) {
      throw new Error("native pipe request exceeds its safety bound");
    }
    const frame = Buffer.allocUnsafe(4 + payload.length);
    frame.writeUInt32LE(payload.length, 0);
    payload.copy(frame, 4);

    const response = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        this.writeCancellation(id);
        reject(new Error(`Codex Desktop ${method} timed out`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
    });
    socket.write(frame);
    return response;
  }

  connect() {
    if (this.socket != null && !this.socket.destroyed) {
      return Promise.resolve();
    }
    if (this.connecting != null) return this.connecting;
    this.connecting = new Promise((resolve, reject) => {
      const socket = net.createConnection(this.pipePath);
      const fail = (error) => {
        socket.destroy();
        reject(error);
      };
      socket.once("error", fail);
      socket.once("connect", () => {
        socket.off("error", fail);
        this.socket = socket;
        this.connecting = null;
        socket.on("data", (chunk) => this.onData(socket, chunk));
        socket.on("error", (error) => this.onDisconnect(socket, error));
        socket.on("close", () =>
          this.onDisconnect(socket, new Error("Codex Desktop native pipe closed")),
        );
        resolve();
      });
    }).catch((error) => {
      this.connecting = null;
      throw error;
    });
    return this.connecting;
  }

  onData(socket, chunk) {
    if (this.socket !== socket) return;
    this.pendingData = Buffer.concat([this.pendingData, chunk]);
    while (this.pendingData.length >= 4) {
      const length = this.pendingData.readUInt32LE(0);
      if (length > MAX_FRAME_BYTES) {
        socket.destroy(new Error("native pipe response exceeds its safety bound"));
        return;
      }
      if (this.pendingData.length < length + 4) return;
      const body = this.pendingData.subarray(4, length + 4);
      this.pendingData = this.pendingData.subarray(length + 4);
      let response;
      try {
        response = JSON.parse(body.toString("utf8"));
      } catch {
        socket.destroy(new Error("native pipe returned invalid JSON"));
        return;
      }
      const pending = this.pending.get(Number(response?.id));
      if (!pending) continue;
      this.pending.delete(Number(response.id));
      if (response.error) {
        pending.reject(
          new Error(
            `Codex Desktop native tool error ${response.error.code}: ${response.error.message}`,
          ),
        );
      } else {
        pending.resolve(response.result);
      }
    }
  }

  onDisconnect(socket, error) {
    if (this.socket !== socket) return;
    this.socket = null;
    this.tools = null;
    this.pendingData = Buffer.alloc(0);
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }

  writeCancellation(id) {
    const socket = this.socket;
    if (socket == null || socket.destroyed) return;
    const payload = Buffer.from(
      JSON.stringify({ jsonrpc: "2.0", id, method: "tools/cancel" }),
      "utf8",
    );
    const frame = Buffer.allocUnsafe(4 + payload.length);
    frame.writeUInt32LE(payload.length, 0);
    payload.copy(frame, 4);
    socket.write(frame);
  }

  close() {
    this.socket?.destroy();
    this.socket = null;
  }
}

function validateClaim(claim) {
  if (
    claim == null ||
    !UUID_PATTERN.test(claim.thread_id) ||
    !UUID_PATTERN.test(claim.context_turn_id) ||
    typeof claim.call_id !== "string" ||
    !claim.call_id.startsWith("durable-continue-") ||
    claim.prompt !== "continue"
  ) {
    throw new Error("dispatcher claim failed native-send validation");
  }
}
