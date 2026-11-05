// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { Metadata } from "next";
import "web-shared/telecom-fonts.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "ACME Mobile",
  description: "Compare plans and manage your line with the ACME Assistant.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="font-mobile">
      <body>
        <div className="am-grid" aria-hidden />
        <div className="relative z-[2] h-full">
          {children}
        </div>
      </body>
    </html>
  );
}
