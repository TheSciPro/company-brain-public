import { describe, expect, it } from "vitest";
import { parseSse } from "./sse";

describe("parseSse", () => {
  it("splits complete events and keeps the partial tail", () => {
    const { events, rest } = parseSse('event: token\ndata: {"text":"Hi"}\n\nevent: fin');
    expect(events).toEqual([{ event: "token", data: '{"text":"Hi"}' }]);
    expect(rest).toBe("event: fin");
  });

  it("ignores pings and handles CRLF and multi-line data", () => {
    const { events } = parseSse(": ping\r\n\r\nevent: final\r\ndata: a\r\ndata: b\r\n\r\n");
    expect(events).toEqual([{ event: "final", data: "a\nb" }]);
  });
});
