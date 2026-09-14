import { describe, expect, it } from "vitest";
import { parseHash } from "./useHashRoute";

describe("parseHash", () => {
  it("defaults to the live screen when the hash is empty", () => {
    expect(parseHash("")).toEqual({ screen: "live", param: null });
  });

  it("parses a bare screen", () => {
    expect(parseHash("#/work")).toEqual({ screen: "work", param: null });
  });

  it("parses a screen with a param", () => {
    expect(parseHash("#/work/abc-123")).toEqual({ screen: "work", param: "abc-123" });
  });

  it("decodes a URL-encoded param", () => {
    expect(parseHash("#/work/abc%2F123")).toEqual({ screen: "work", param: "abc/123" });
  });
});
