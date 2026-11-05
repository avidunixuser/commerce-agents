// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { Metadata } from "next";
import "web-shared/travel-fonts.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "ACME Travel",
  description: "Plan and book trips with the ACME Assistant.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="font-travel">
      <body>
        {/* Grain at z-1; content above it at z-2. */}
        <div className="al-grain" aria-hidden />
        <div className="relative z-[2] h-full">
          {children}
        </div>
      </body>
    </html>
  );
}
