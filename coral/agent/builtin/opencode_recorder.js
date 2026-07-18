import { appendFile } from "node:fs/promises"

const path = process.env.CORAL_TRACE_PATH
const actorID = process.env.CORAL_AGENT_ID || "unknown"
const active = new Map()

function nowNs() {
  return (BigInt(Date.now()) * 1000000n).toString()
}

async function append(record) {
  if (!path) return
  try {
    await appendFile(path, JSON.stringify({
      schema_version: "coral.runtime-span/v1",
      kind: "opencode.tool",
      actor_id: actorID,
      pid: process.pid,
      ...record,
    }) + "\n")
  } catch {
    // Recording is observational and must not affect tool execution.
  }
}

export default async () => ({
  "tool.execute.before": async (input) => {
    const startedAtNs = nowNs()
    active.set(input.callID, {
      tool: input.tool,
      sessionID: input.sessionID,
      startedAtNs,
    })
    await append({
      phase: "start",
      tool: input.tool,
      call_id: input.callID,
      session_id: input.sessionID,
      started_at_ns: startedAtNs,
    })
  },

  "tool.execute.after": async (input) => {
    const start = active.get(input.callID)
    active.delete(input.callID)
    await append({
      phase: "end",
      tool: input.tool,
      call_id: input.callID,
      session_id: input.sessionID,
      started_at_ns: start?.startedAtNs,
      ended_at_ns: nowNs(),
      status: "success",
    })
  },

  "shell.env": async (input, output) => {
    if (input.callID) output.env.CORAL_PARENT_CALL_ID = input.callID
    if (input.sessionID) output.env.CORAL_PARENT_SESSION_ID = input.sessionID
    output.env.CORAL_AGENT_ID = actorID
    if (path) output.env.CORAL_TRACE_PATH = path
  },

  event: async ({ event }) => {
    if (event.type !== "message.part.updated") return
    const part = event.properties?.part
    if (part?.type !== "tool" || part.state?.status !== "error") return
    const start = active.get(part.callID)
    if (!start) return
    active.delete(part.callID)
    await append({
      phase: "end",
      tool: part.tool,
      call_id: part.callID,
      session_id: part.sessionID,
      started_at_ns: start.startedAtNs,
      ended_at_ns: nowNs(),
      status: "error",
    })
  },
})
