// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { Metadata } from "next";
import "web-shared/travel-fonts.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Zava Travel Merchant",
  description: "The Zava Travel back office, an example for the merchant agent.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="font-travel">
      <body>
        {children}
      </body>
    </html>
  );
}
