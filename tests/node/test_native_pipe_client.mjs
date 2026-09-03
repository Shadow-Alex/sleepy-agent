import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import test from "node:test";
import { randomUUID } from "node:crypto";

import { NativePipeClient } from "../../mcp/native_pipe_client.mjs";

const THREAD_ID = "019f0000-0000-7000-8000-000000000010";
const TURN_ID = "019f0000-0000-7000-8000-000000000011";

test("native client can only call exact Desktop send_message_to_thread", async () => {
  const directory = "/tmp/codex-browser-use";
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const socketPath = `${directory}/durable-continue-test-${randomUUID()}.sock`;
  const requests = [];
  const server = net.createServer((socket) => {
    let pending = Buffer.alloc(0);
    socket.on("data", (chunk) => {
      pending = Buffer.concat([pending, chunk]);
      while (pending.length >= 4) {
        const length = pending.readUInt32LE(0);
        if (pending.length < length + 4) return;
        const request = JSON.parse(
          pending.subarray(4, length + 4).toString("utf8"),
        );
        pending = pending.subarray(length + 4);
        requests.push(request);
        if (request.method === "tools/list") {
          respond(socket, request.id, {
            tools: [
              {
                name: "send_message_to_thread",
                namespace: "codex_app",
              },
            ],
          });
        } else if (request.method === "tools/call") {
          respond(socket, request.id, {
            success: true,
            contentItems: [{ type: "inputText", text: "sent" }],
          });
        }
      }
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });

  const client = new NativePipeClient(socketPath);
  try {
    await client.sendContinue({
      thread_id: THREAD_ID,
      context_turn_id: TURN_ID,
      call_id: "durable-continue-client-id",
      prompt: "continue",
    });
  } finally {
    client.close();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(socketPath, { force: true });
  }

  assert.equal(requests.length, 2);
  assert.equal(requests[0].method, "tools/list");
  assert.deepEqual(requests[1], {
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: {
      arguments: { threadId: THREAD_ID, prompt: "continue" },
      callId: "durable-continue-client-id",
      namespace: "codex_app",
      threadId: THREAD_ID,
      tool: "send_message_to_thread",
      turnId: TURN_ID,
    },
  });
});

test("native client rejects any non-exact prompt before connecting", async () => {
  const client = new NativePipeClient(
    `/tmp/codex-browser-use/durable-continue-test-${randomUUID()}.sock`,
  );
  await assert.rejects(
    client.sendContinue({
      thread_id: THREAD_ID,
      context_turn_id: TURN_ID,
      call_id: "durable-continue-client-id",
      prompt: "continue please",
    }),
    /claim failed native-send validation/,
  );
});

function respond(socket, id, result) {
  const body = Buffer.from(JSON.stringify({ jsonrpc: "2.0", id, result }), "utf8");
  const frame = Buffer.allocUnsafe(4 + body.length);
  frame.writeUInt32LE(body.length, 0);
  body.copy(frame, 4);
  socket.write(frame);
}
