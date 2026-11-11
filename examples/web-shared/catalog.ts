// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useState } from "react";
import { useStoreFrame } from "./storefront/frame";

type Loader<P> = () => Promise<P[] | null>;

interface CachedIndex {
  session: string | undefined;
  promise: Promise<Record<string, unknown> | null>;
}

const indexes = new WeakMap<object, CachedIndex>();

/** Shared within a session; failed reads are not cached across mounts or session changes. */
export function useCatalogIndex<P extends { product_id: string }>(
  load: Loader<P>,
  sessionId?: string | null,
): Record<string, P> {
  const frame = useStoreFrame();
  const session = sessionId === undefined ? frame.catalogSession : sessionId;
  const [index, setIndex] = useState<{
    load: Loader<P>;
    session: string | null | undefined;
    value: Record<string, P>;
  } | null>(null);
  useEffect(() => {
    if (session === null) return;
    let cached = indexes.get(load);
    if (!cached || cached.session !== session) {
      const promise = load().then((products) =>
        products === null ? null : Object.fromEntries(products.map((product) => [product.product_id, product])),
      );
      cached = { session, promise };
      indexes.set(load, cached);
    }
    const current = cached;
    let mounted = true;
    void current.promise.then((value) => {
      if (value === null && indexes.get(load) === current) indexes.delete(load);
      if (mounted) setIndex({ load, session, value: (value ?? {}) as Record<string, P> });
    }, (error) => {
      if (indexes.get(load) === current) indexes.delete(load);
      if (mounted) window.dispatchEvent(new CustomEvent("commerce-api-error", {
        detail: error instanceof Error ? error.message : "Catalog load failed.",
      }));
    });
    return () => {
      mounted = false;
    };
  }, [load, session]);
  return index?.load === load && index.session === session ? index.value : {};
}
