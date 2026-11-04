// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { Metadata } from "next";
import "web-shared/entertainment-fonts.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "ACME Tickets",
  description: "Find shows and hold seats with the ACME Assistant.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className="font-tickets"
    >
      <body>
        {/* Page-wide limelight wash. */}
        <div className="at-house" aria-hidden />
        <div className="relative z-[2] h-full">
          {children}
        </div>
      </body>
    </html>
  );
}
