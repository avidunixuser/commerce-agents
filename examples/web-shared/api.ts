// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { AgentEvent, MemoryFact, Order } from "./protocol";

const SESSION_HEADER = "X-Session-Id";

/** Immutable production builds must use the BFF, never a browser-to-API fallback. */
export function apiRoot(demoRoot: string): string {
  if (process.env.NODE_ENV === "production") return "";
  return process.env.NEXT_PUBLIC_API_URL ?? demoRoot;
}

/**
 * The client both roles' web apps use; the storefront's cart, orders, and memory reads live here
 * too. The session token travels only in the session header. Reads return null on any failure
 * so callers keep their last good state.
 */
export class AgentApi {
  session: string | null = null;
  readonly base: string;
  csrf: string | null = null;
  private activeChat: AbortController | null = null;
  private activeTurn: string | null = null;
  private pendingActions = new Map<string, string>();
  private actionRequests = new Map<string, Promise<unknown | null>>();

  /** `root` is the API's URL; `prefix` the role's route prefix ("/api", "/api/merchant"). */
  constructor(
    readonly root: string,
    prefix: string,
  ) {
    this.base = `${root}${prefix}`;
  }

  /** Files the API serves by path ("/products/AR-1002.webp"). */
  assetUrl(path: string | null | undefined): string | null {
    if (!path) return null;
    return path.startsWith("/") ? `${this.root}${path}` : path;
  }

  headers(json = false): Record<string, string> {
    const headers: Record<string, string> = {};
    if (this.session) headers[SESSION_HEADER] = this.session;
    if (json) headers["Content-Type"] = "application/json";
    if (this.csrf) headers["X-CSRF-Token"] = this.csrf;
    return headers;
  }

  async get<T>(path: string, params?: Record<string, string>): Promise<T | null> {
    const query = params && Object.keys(params).length ? `?${new URLSearchParams(params)}` : "";
    return this.request<T>(`${path}${query}`, { headers: this.headers() });
  }

  async post<T>(path: string, body?: unknown): Promise<T | null> {
    return this.send<T>("POST", path, body);
  }

  async patch<T>(path: string, body: unknown): Promise<T | null> {
    return this.send<T>("PATCH", path, body);
  }

  async delete<T>(path: string, body?: unknown): Promise<T | null> {
    return this.send<T>("DELETE", path, body);
  }

  private async send<T>(method: string, path: string, body?: unknown): Promise<T | null> {
    const session = this.session;
    const key = JSON.stringify([session, method, path, body]);
    const active = this.actionRequests.get(key);
    if (active) return active as Promise<T | null>;
    if (this.pendingActions.size >= 100 && !this.pendingActions.has(key)) {
      this.report("error", "Too many pending actions. Resolve their status before submitting more.");
      return null;
    }
    const run = async () => {
      const turnId = this.pendingActions.get(key);
      if (this.pendingActions.has(key)) {
        if (!turnId) {
          this.report("error", "This action was accepted without a durable identifier. Reconcile its outcome before submitting it again.");
          return null;
        }
        try { return await this.waitForAction<T>(turnId, key, session); }
        catch (error) {
          this.report("error", error instanceof Error ? error.message : "Action status check failed.");
          return null;
        }
      }
      return this.request<T>(path, {
        method,
        headers: { ...this.headers(body !== undefined), "Idempotency-Key": crypto.randomUUID() },
        body: body === undefined ? undefined : JSON.stringify(body),
      }, key, session);
    };
    const promise = run();
    this.actionRequests.set(key, promise);
    try { return await promise; }
    finally { this.actionRequests.delete(key); }
  }

  private report(kind: "error" | "notice", message: string): void {
    if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(`commerce-api-${kind}`, {
      detail: message,
    }));
  }

  private async waitForAction<T>(turnId: string, key: string, session: string | null): Promise<T | null> {
    for (let attempt = 0; attempt < 120; attempt++) {
      if (session !== this.session) throw new Error("The session changed while an action was pending. It was not resubmitted.");
      const response = await fetch(`${this.base}/turns/${encodeURIComponent(turnId)}`, {
        credentials: "same-origin", cache: "no-store", headers: this.headers(),
      });
      if (!response.ok) throw new Error(`Action status check failed (${response.status}). The action was not resubmitted.`);
      const job = await response.json();
      if (job.status === "complete") {
        if (!Object.prototype.hasOwnProperty.call(job, "result")) throw new Error("The completed action has no confirmed result. Refresh its status before retrying.");
        this.pendingActions.delete(key);
        this.report("notice", "Request completed.");
        return job.result as T;
      }
      if (["failed", "dead", "cancelled", "uncertain"].includes(job.status)) {
        throw new Error(`Action status: ${job.status}. Check or reconcile its outcome; it was not resubmitted.`);
      }
      if (!["accepted", "pending", "ready", "running", "retry"].includes(job.status)) {
        throw new Error("The action returned an unknown status. It was not resubmitted.");
      }
      if (attempt < 119) await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    throw new Error("The action is still pending. Check again to resume status polling without submitting a duplicate.");
  }

  private async request<T>(path: string, init: RequestInit, actionKey?: string, session = this.session): Promise<T | null> {
    try {
      const response = await fetch(`${this.base}${path}`, { ...init, credentials: "same-origin" });
      if (!response.ok) throw new Error(`Request failed (${response.status}). Please retry or sign in again.`);
      if (response.status === 202) {
        const accepted = await response.json();
        if (!actionKey || typeof accepted.turn_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(accepted.turn_id)) {
          if (actionKey) this.pendingActions.set(actionKey, "");
          throw new Error("The request was accepted without a durable status identifier. Do not blindly retry it.");
        }
        this.pendingActions.set(actionKey, accepted.turn_id);
        this.report("notice", "Request accepted for processing, not yet applied. Checking its durable status.");
        return await this.waitForAction<T>(accepted.turn_id, actionKey, session);
      }
      return (await response.json()) as T;
    } catch (error) {
      this.report("error", error instanceof Error ? error.message : "The request failed.");
      return null;
    }
  }

  /** A storefront passes its profile as `{ user_id }` and gets the shopper's name back; a merchant session names its operator. */
  async startSession(body?: Record<string, unknown>): Promise<{ sessionId: string; operator?: string; shopper?: { name: string; tier?: string } } | null> {
    let productionIdentity: { kind?: string; role?: string } | undefined;
    if (this.root === "") {
      const auth = await fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" });
      if (!auth.ok) throw new Error("Unable to verify browser session");
      const state = await auth.json();
      if (!state.authenticated) return null;
      productionIdentity = state;
      this.csrf = state.csrf_token;
      // Browser demo profile names never select a production principal.
      body = undefined;
    }
    const data = await this.post<{ session_id: string; operator?: string; name?: string | null; tier?: string | null }>("/session", body);
    if (!data?.session_id) return null;
    const shopper = data.name ? { name: data.name, tier: data.tier ?? undefined }
      : productionIdentity?.role === "shopping" ? { name: productionIdentity.kind === "guest" ? "Guest" : "Signed-in customer" }
      : undefined;
    return { sessionId: data.session_id, operator: data.operator ?? (productionIdentity?.role === "merchant" ? "Merchant operator" : undefined), shopper };
  }

  async fetchMemory(): Promise<MemoryFact[] | null> {
    const data = await this.get<{ facts?: MemoryFact[] }>("/memory");
    return data ? (data.facts ?? []) : null;
  }

  /** The corrected fact, or null when the store refused the value. */
  async editMemoryFact(key: string, value: string): Promise<MemoryFact | null> {
    const data = await this.patch<{ fact: MemoryFact }>("/memory", { key, value });
    return data?.fact ?? null;
  }

  async forgetMemoryFact(key: string): Promise<boolean> {
    return (await this.delete<{ ok: boolean }>("/memory", { key })) !== null;
  }

  /** The bag as the vertical's API shapes it (lines, count, subtotal, plus its own extras). */
  async fetchCart<T>(): Promise<T | null> {
    return this.get<T>("/cart");
  }

  /** The signed-in customer's orders, newest first. */
  async fetchOrders(): Promise<Order[] | null> {
    const data = await this.get<{ orders: Order[] }>("/orders");
    return data?.orders ?? null;
  }

  /** Throws when the request itself fails. */
  async cancelChat(): Promise<void> {
    this.activeChat?.abort();
    if (this.activeTurn) await this.post(`/turns/${encodeURIComponent(this.activeTurn)}/cancel`);
  }

  async *chatStream(message: string): AsyncGenerator<AgentEvent> {
    const controller = new AbortController();
    this.activeChat = controller;
    this.activeTurn = null;
    let cursor = "";
    let complete = false;
    let attempts = 0;
    const seen = new Set<string>();
    try {
      let response = await fetch(`${this.base}/chat`, {
        method: "POST", credentials: "same-origin", signal: controller.signal,
        headers: { ...this.headers(true), "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ message }),
      });
      this.activeTurn = response.headers.get("X-Turn-Id");
      while (true) {
        if (!response.ok || !response.body) throw new Error(`Chat request failed (${response.status})`);
        try {
          for await (const frame of readEventStream(response.body)) {
            if (frame.id) {
              if (seen.has(frame.id)) continue;
              seen.add(frame.id);
              if (seen.size > 10000) throw new Error("Turn event limit exceeded.");
              cursor = frame.id;
            }
            if (frame.event.data.turn_id) this.activeTurn = String(frame.event.data.turn_id);
            if (frame.event.type === "turn_complete") complete = true;
            if (frame.event.type === "error" && ["failed", "dead", "cancelled", "uncertain"].includes(String(frame.event.data.status))) complete = true;
            yield frame.event;
          }
        } catch (error) {
          if (controller.signal.aborted || !this.activeTurn) throw error;
        }
        if (complete) return;
        // Demo streams have no durable turn identifier. Never replay a chat POST.
        if (!this.activeTurn) {
          if (this.root === "") throw new Error("The stream ended without a durable turn identifier.");
          return;
        }
        if (++attempts > 8) throw new Error("Stream reconnect limit reached. The turn was not resubmitted.");
        await new Promise<void>((resolve, reject) => {
          const abort = () => { clearTimeout(timer); reject(new DOMException("Cancelled", "AbortError")); };
          const timer = setTimeout(() => {
            controller.signal.removeEventListener("abort", abort);
            resolve();
          }, Math.min(500 * 2 ** (attempts - 1), 5000));
          controller.signal.addEventListener("abort", abort, { once: true });
          if (controller.signal.aborted) abort();
        });
        response = await fetch(`${this.base}/turns/${encodeURIComponent(this.activeTurn)}/events`, {
          credentials: "same-origin", signal: controller.signal,
          headers: { ...this.headers(), ...(cursor ? { "Last-Event-ID": cursor } : {}) },
        });
      }
    } finally {
      controller.abort();
      if (this.activeChat === controller) {
        this.activeChat = null;
        this.activeTurn = null;
      }
    }
  }
}

export async function* readEventStream(body: ReadableStream<Uint8Array>): AsyncGenerator<{ id?: string; event: AgentEvent }> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventType = "message";
  let id: string | undefined;
  let data: string[] = [];
  let dataBytes = 0;
  try { while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline: number;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      buffer = buffer.slice(newline + 1);
      if (line.startsWith("event:")) {
        eventType = line.slice(6).trimStart();
      } else if (line.startsWith("id:")) {
        const value = line.slice(3).trimStart();
        if (!value.includes("\0")) id = value;
      } else if (line.startsWith("data:")) {
        data.push(line.slice(5).replace(/^ /, ""));
        dataBytes += line.length;
        if (dataBytes > 1048576) throw new Error("SSE frame exceeded size limit");
      } else if (line === "") {
        if (data.length) {
          const payload = JSON.parse(data.join("\n"));
          yield { id, event: { type: eventType, data: payload } as AgentEvent };
        }
        eventType = "message";
        data = [];
        dataBytes = 0;
        id = undefined;
      }
    }
    if (buffer.length > 1048576) throw new Error("SSE frame exceeded size limit");
  } } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}
