export type RunRequest = {
  task: string;
  auto_approve?: boolean;
};

export type RunResult = {
  status: "success" | "partial" | "failed";
  output: string;
  session_id: string;
  run_id: string;
  artifacts: Array<{ path: string; content: string; action: string }>;
  metrics: Record<string, number>;
  run_score: Record<string, unknown> | null;
};

export type StreamItem =
  | { type: "event"; event: Record<string, unknown> }
  | { type: "done"; result: RunResult }
  | { type: "error"; error: string };

export class SynapseClient {
  constructor(private readonly baseUrl = "http://127.0.0.1:8000") {}

  async run(request: RunRequest, signal?: AbortSignal): Promise<RunResult> {
    const response = await fetch(`${this.baseUrl}/run`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request),
      signal,
    });
    if (!response.ok) throw new Error(`Synapse HTTP ${response.status}`);
    return response.json() as Promise<RunResult>;
  }

  async *stream(request: RunRequest, signal?: AbortSignal): AsyncGenerator<StreamItem> {
    const response = await fetch(`${this.baseUrl}/run/stream`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request),
      signal,
    });
    if (!response.ok || !response.body) {
      throw new Error(`Synapse stream HTTP ${response.status}`);
    }

    const decoder = new TextDecoder();
    let buffer = "";
    for await (const chunk of response.body) {
      buffer += decoder.decode(chunk, { stream: true }).replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const payload = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (payload) yield JSON.parse(payload) as StreamItem;
      }
    }
  }
}
