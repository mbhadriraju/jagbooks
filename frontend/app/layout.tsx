import type { Metadata } from "next";
import { FinanceShell } from "@/components/finance/session";
import "./globals.css";

export const metadata: Metadata = {
  title: "JagBooks",
  description: "Financial statements and source documents for Blue Valley West Debate.",
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a className="skip" href="#main">Skip to content</a>
        <FinanceShell>{children}</FinanceShell>
      </body>
    </html>
  );
}
