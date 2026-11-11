// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { Metadata } from "next";
import "web-shared/retail-fonts.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Zava",
  description: "Shop the Zava catalog with the Zava Assistant.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="font-retail">
      <body>{children}</body>
    </html>
  );
}
