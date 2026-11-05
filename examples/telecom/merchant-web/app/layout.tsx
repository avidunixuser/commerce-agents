// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { Metadata } from "next";
import "web-shared/telecom-fonts.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "ACME Mobile Merchant",
  description: "The ACME Mobile back office, an example for the merchant agent.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="font-mobile">
      <body>{children}</body>
    </html>
  );
}
