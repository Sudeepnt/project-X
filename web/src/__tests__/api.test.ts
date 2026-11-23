import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "@/lib/api";

afterEach(() => {
  vi.restoreAllMocks();
  document.cookie = "px_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

describe("api client", () => {
  it("sends the CSRF token on writes but not reads", async () => {
    document.cookie = "px_csrf=tok123";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", { status: 200 }));
    await api.get("/api/v1/x");
    await api.post("/api/v1/x", { a: 1 });
    const [, getInit] = fetchMock.mock.calls[0]!;
    const [, postInit] = fetchMock.mock.calls[1]!;
    expect((getInit!.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
    expect((postInit!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok123");
    expect(postInit!.body).toBe('{"a":1}');
    expect(postInit!.credentials).toBe("same-origin");
  });

  it("turns problem+json into an ApiError with the server's message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: 409, code: "conflict", detail: "This decision is already approved" }), {
        status: 409,
      }),
    );
    const err = (await api.post("/api/v1/x").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.code).toBe("conflict");
    expect(err.message).toBe("This decision is already approved");
  });

  it("handles 204 No Content", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api.delete("/api/v1/x")).resolves.toBeUndefined();
  });

  it("does not set a JSON content type for uploads", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    const form = new FormData();
    form.append("file", new Blob(["a,b"]), "x.csv");
    await api.post("/api/v1/upload", form);
    const [, init] = fetchMock.mock.calls[0]!;
    expect((init!.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
    expect(init!.body).toBe(form);
  });
});
